-- =====================================================================
-- G5 - MANAGEMENT SQL for finding 3-4  (v4, surgical, gated, transactional)
-- RUN ONLY AS avnadmin, ONLY AFTER THE PREFLIGHT PASSED AND A HUMAN APPROVED.
-- =====================================================================
-- Scope of the change - exactly one privilege bit, plus two surgical grants:
--     GRANT TEMPORARY ON DATABASE defaultdb TO _aiven_repl     (NEW in v4)
--     GRANT TEMPORARY ON DATABASE defaultdb TO _aiven_rewind    (NEW in v4)
--     REVOKE TEMPORARY ON DATABASE defaultdb FROM PUBLIC
-- Why v4: the v3 revoke (FROM PUBLIC) would also strip TEMPORARY from the two
-- Aiven-internal roles _aiven_repl and _aiven_rewind, which currently inherit
-- it only through PUBLIC. v4 grants it to them explicitly first, so after the
-- revoke only audit_ro loses TEMPORARY (the actual finding). CONNECT stays with
-- PUBLIC; CREATE is not touched; no table, schema, data, function, ownership or
-- role attribute changes.
--
-- Safety design:
--   1. guards refuse to run on the wrong role / database / a standby, or if
--      audit_ro / _aiven_repl / _aiven_rewind do not currently hold TEMPORARY
--      (cluster topology changed -> stop for human review);
--   2. the ACL statements run inside an explicit transaction;
--   3. a DO block asserts the post-conditions INSIDE the transaction: if any
--      assert fails it RAISEs, the transaction aborts and psql (ON_ERROR_STOP)
--      exits, so nothing is committed - the ACL is rolled back automatically;
--   4. the AFTER snapshot is printed after COMMIT for the record.
--
-- Deviation from v3 (reviewed 2026-11-19): v3 revoked PUBLIC.TEMPORARY only,
-- which would also affect _aiven_repl and _aiven_rewind. v4 adds two explicit
-- grants before the revoke so the blast radius is limited to audit_ro. The
-- revoke itself is identical.
--
-- Run (verify-full, all connections):
--   psql "host=pg-2f0e4f1c-newsbot-db.e.aivencloud.com port=27362 dbname=defaultdb user=avnadmin sslmode=verify-full sslrootcert=C:/g5_fix5/aiven-ca.pem" -v ON_ERROR_STOP=1 -f FIX_AUDIT_RO_v4.sql
-- Or, when libpq rejects Aiven's certificate (malformed IP SAN defect):
--   python g5_admin.py --mode fix --apply-fix --sql FIX_AUDIT_RO_v4.sql --ca aiven-ca.pem --user avnadmin
-- =====================================================================

\echo '=== 0. GUARDS (raise = abort before any change) ==='
DO $g5_guards$
BEGIN
    IF current_user <> 'avnadmin' THEN
        RAISE EXCEPTION 'G5: must run as avnadmin, current_user=%', current_user;
    END IF;
    IF current_database() <> 'defaultdb' THEN
        RAISE EXCEPTION 'G5: must run on defaultdb, current_database=%', current_database();
    END IF;
    IF pg_is_in_recovery() THEN
        RAISE EXCEPTION 'G5: server is in recovery; run on the primary only';
    END IF;
    IF has_database_privilege('audit_ro', current_database(), 'TEMPORARY') IS NOT TRUE THEN
        RAISE EXCEPTION 'G5: audit_ro already has no TEMPORARY - preflight expectation broken, stop';
    END IF;
    -- v4: the two Aiven-internal roles must exist and currently hold TEMPORARY
    -- (via PUBLIC). If either is missing or already stripped, topology changed
    -- -> stop for human review instead of silently skipping.
    IF to_regrole('_aiven_repl') IS NULL THEN
        RAISE EXCEPTION 'G5: role _aiven_repl does not exist - cluster topology changed, stop';
    END IF;
    IF to_regrole('_aiven_rewind') IS NULL THEN
        RAISE EXCEPTION 'G5: role _aiven_rewind does not exist - cluster topology changed, stop';
    END IF;
    IF NOT has_database_privilege('_aiven_repl', current_database(), 'TEMPORARY') THEN
        RAISE EXCEPTION 'G5: _aiven_repl has no TEMPORARY before fix - already modified or unexpected, stop';
    END IF;
    IF NOT has_database_privilege('_aiven_rewind', current_database(), 'TEMPORARY') THEN
        RAISE EXCEPTION 'G5: _aiven_rewind has no TEMPORARY before fix - already modified or unexpected, stop';
    END IF;
END
$g5_guards$;

\echo '=== 1. BEFORE (expected: audit_ro=t, avnadmin=t, monitor=t, repl=t, rewind=t, public_temp=t) ==='
SELECT has_database_privilege('audit_ro',          current_database(), 'TEMPORARY') AS audit_ro_temp_before,
       has_database_privilege('avnadmin',          current_database(), 'TEMPORARY') AS avnadmin_temp_before,
       has_database_privilege('_avnadmin_monitor', current_database(), 'TEMPORARY') AS monitor_temp_before,
       has_database_privilege('_aiven_repl',       current_database(), 'TEMPORARY') AS repl_temp_before,
       has_database_privilege('_aiven_rewind',     current_database(), 'TEMPORARY') AS rewind_temp_before,
       has_database_privilege('public',            current_database(), 'TEMPORARY') AS public_temp_before;

SELECT datacl::text AS datacl_before FROM pg_database WHERE datname = current_database();

\echo '=== 2. CHANGE (surgical: explicit grants first, then the PUBLIC revoke) ==='
BEGIN;

-- (a) Safeguard, conditional: never touch the ACL when avnadmin already has it.
DO $g5_grant_admin$
BEGIN
    IF NOT has_database_privilege('avnadmin', current_database(), 'TEMPORARY') THEN
        EXECUTE format('GRANT TEMPORARY ON DATABASE %I TO avnadmin', current_database());
        RAISE NOTICE 'G5: issued GRANT TEMPORARY to avnadmin (was missing)';
    ELSE
        RAISE NOTICE 'G5: avnadmin already holds TEMPORARY explicitly - no GRANT issued';
    END IF;
END
$g5_grant_admin$;

-- (b) v4 surgical: give _aiven_repl an explicit TEMPORARY grant (it currently
--     has it only via PUBLIC). Idempotent: skipped if an explicit entry already
--     exists in the database ACL.
DO $g5_grant_repl$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM aclexplode((SELECT datacl FROM pg_database WHERE datname = current_database())) AS x
        WHERE x.grantee = (SELECT oid FROM pg_roles WHERE rolname = '_aiven_repl')
          AND x.privilege_type = 'TEMPORARY'
    ) THEN
        EXECUTE format('GRANT TEMPORARY ON DATABASE %I TO %I', current_database(), '_aiven_repl');
        RAISE NOTICE 'G5: issued GRANT TEMPORARY to _aiven_repl (was inherited via PUBLIC only)';
    ELSE
        RAISE NOTICE 'G5: _aiven_repl already holds TEMPORARY explicitly - no GRANT issued';
    END IF;
END
$g5_grant_repl$;

-- (c) v4 surgical: same for _aiven_rewind.
DO $g5_grant_rewind$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM aclexplode((SELECT datacl FROM pg_database WHERE datname = current_database())) AS x
        WHERE x.grantee = (SELECT oid FROM pg_roles WHERE rolname = '_aiven_rewind')
          AND x.privilege_type = 'TEMPORARY'
    ) THEN
        EXECUTE format('GRANT TEMPORARY ON DATABASE %I TO %I', current_database(), '_aiven_rewind');
        RAISE NOTICE 'G5: issued GRANT TEMPORARY to _aiven_rewind (was inherited via PUBLIC only)';
    ELSE
        RAISE NOTICE 'G5: _aiven_rewind already holds TEMPORARY explicitly - no GRANT issued';
    END IF;
END
$g5_grant_rewind$;

-- (d) The actual fix. A per-role DENY does not exist in PostgreSQL, so revoking
--     from audit_ro alone would be a no-op: the privilege comes from PUBLIC.
REVOKE TEMPORARY ON DATABASE defaultdb FROM PUBLIC;

\echo '=== 3. POST-CONDITIONS, ASSERTED INSIDE THE TRANSACTION (violation = rollback) ==='
DO $g5_assert$
BEGIN
    -- audit_ro must lose TEMPORARY (the finding).
    IF has_database_privilege('audit_ro', current_database(), 'TEMPORARY') THEN
        RAISE EXCEPTION 'G5: audit_ro still has TEMPORARY after REVOKE';
    END IF;
    -- application / provider roles must keep TEMPORARY.
    IF NOT has_database_privilege('avnadmin', current_database(), 'TEMPORARY') THEN
        RAISE EXCEPTION 'G5: avnadmin lost TEMPORARY - application role would break';
    END IF;
    IF NOT has_database_privilege('_avnadmin_monitor', current_database(), 'TEMPORARY') THEN
        RAISE EXCEPTION 'G5: _avnadmin_monitor lost TEMPORARY - provider role would break';
    END IF;
    -- v4: the two Aiven-internal roles must keep TEMPORARY via the explicit grant.
    IF NOT has_database_privilege('_aiven_repl', current_database(), 'TEMPORARY') THEN
        RAISE EXCEPTION 'G5: _aiven_repl lost TEMPORARY - Aiven replication role would break';
    END IF;
    IF NOT has_database_privilege('_aiven_rewind', current_database(), 'TEMPORARY') THEN
        RAISE EXCEPTION 'G5: _aiven_rewind lost TEMPORARY - Aiven rewind role would break';
    END IF;
    -- v4: confirm the grant is EXPLICIT (not via PUBLIC), since PUBLIC is revoked.
    IF NOT EXISTS (
        SELECT 1 FROM aclexplode((SELECT datacl FROM pg_database WHERE datname = current_database())) AS x
        WHERE x.grantee = (SELECT oid FROM pg_roles WHERE rolname = '_aiven_repl') AND x.privilege_type = 'TEMPORARY'
    ) THEN
        RAISE EXCEPTION 'G5: _aiven_repl has no explicit TEMPORARY entry - surgical grant did not take effect';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM aclexplode((SELECT datacl FROM pg_database WHERE datname = current_database())) AS x
        WHERE x.grantee = (SELECT oid FROM pg_roles WHERE rolname = '_aiven_rewind') AND x.privilege_type = 'TEMPORARY'
    ) THEN
        RAISE EXCEPTION 'G5: _aiven_rewind has no explicit TEMPORARY entry - surgical grant did not take effect';
    END IF;
    -- PUBLIC must lose TEMPORARY but keep CONNECT; DDL privilege untouched.
    IF has_database_privilege('public', current_database(), 'TEMPORARY') THEN
        RAISE EXCEPTION 'G5: PUBLIC still has TEMPORARY';
    END IF;
    IF NOT has_database_privilege('public', current_database(), 'CONNECT') THEN
        RAISE EXCEPTION 'G5: PUBLIC lost CONNECT - unintended ACL change';
    END IF;
    IF has_database_privilege('public', current_database(), 'CREATE') THEN
        RAISE EXCEPTION 'G5: PUBLIC unexpectedly has CREATE';
    END IF;
    IF EXISTS (
        SELECT 1
        FROM aclexplode((SELECT datacl FROM pg_database WHERE datname = current_database())) AS x
        WHERE x.grantee = 0 AND x.privilege_type = 'TEMPORARY'
    ) THEN
        RAISE EXCEPTION 'G5: the PUBLIC TEMPORARY entry still exists in datacl';
    END IF;
    RAISE NOTICE 'G5: all post-conditions hold; committing';
END
$g5_assert$;

COMMIT;

\echo '=== 4. AFTER (expected: audit_ro=f, avnadmin=t, monitor=t, repl=t, rewind=t, public_temp=f, public_connect=t) ==='
SELECT has_database_privilege('audit_ro',          current_database(), 'TEMPORARY') AS audit_ro_temp_after,
       has_database_privilege('avnadmin',          current_database(), 'TEMPORARY') AS avnadmin_temp_after,
       has_database_privilege('_avnadmin_monitor', current_database(), 'TEMPORARY') AS monitor_temp_after,
       has_database_privilege('_aiven_repl',       current_database(), 'TEMPORARY') AS repl_temp_after,
       has_database_privilege('_aiven_rewind',     current_database(), 'TEMPORARY') AS rewind_temp_after,
       has_database_privilege('public',            current_database(), 'TEMPORARY') AS public_temp_after,
       has_database_privilege('public',            current_database(), 'CONNECT')   AS public_connect_after;

\echo '=== 5. AFTER datacl (expected: {=c/avnadmin,avnadmin=CTc/avnadmin,_avnadmin_monitor=Tc/avnadmin,_aiven_repl=T/avnadmin,_aiven_rewind=T/avnadmin}) ==='
SELECT datacl::text AS datacl_after FROM pg_database WHERE datname = current_database();

\echo '=== END (only PUBLIC.TEMPORARY changed; _aiven_repl/_aiven_rewind keep TEMP via explicit grant; now re-run RUN_G5_REAUDIT_fix5.ps1) ==='
