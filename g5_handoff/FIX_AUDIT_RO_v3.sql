-- =====================================================================
-- G5 - MANAGEMENT SQL for finding 3-4  (v3, gated, transactional)
-- RUN ONLY AS avnadmin, ONLY AFTER THE PREFLIGHT PASSED AND A HUMAN APPROVED.
-- =====================================================================
-- Scope of the change - exactly one privilege bit:
--     REVOKE TEMPORARY ON DATABASE defaultdb FROM PUBLIC
-- The GRANT below is conditional and is issued ONLY if avnadmin somehow lacks
-- the explicit TEMPORARY grant; on this cluster it is a no-op, so the only ACL
-- mutation is PUBLIC's TEMPORARY bit. CONNECT stays with PUBLIC; CREATE is not
-- touched; no table, schema, data, function, ownership or role attribute changes.
--
-- Safety design:
--   1. guards refuse to run on the wrong role / database / a standby;
--   2. the two ACL statements run inside an explicit transaction;
--   3. a DO block asserts the post-conditions INSIDE the transaction: if any
--      assert fails it RAISEs, the transaction aborts and psql (ON_ERROR_STOP)
--      exits, so nothing is committed - the ACL is rolled back automatically;
--   4. the AFTER snapshot is printed after COMMIT for the record.
--
-- Deviation from v2 (reviewed 2026-11-19): v2 ran the GRANT unconditionally and
-- had no rollback path. v3 makes the GRANT conditional and commits only after
-- the asserts pass. The two ACL statements themselves are identical.
--
-- Run (verify-full, all connections):
--   psql "host=pg-2f0e4f1c-newsbot-db.e.aivencloud.com port=27362 dbname=defaultdb user=avnadmin sslmode=verify-full sslrootcert=C:/g5_fix4/aiven-ca.pem" -v ON_ERROR_STOP=1 -f FIX_AUDIT_RO_v3.sql
-- Or, when libpq rejects Aiven's certificate (malformed IP SAN defect):
--   python g5_admin.py --mode fix --apply-fix --sql FIX_AUDIT_RO_v3.sql --ca aiven-ca.pem --user avnadmin
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
END
$g5_guards$;

\echo '=== 1. BEFORE (expected: audit_ro=t, avnadmin=t, monitor=t, public_temp=t) ==='
SELECT has_database_privilege('audit_ro',         current_database(), 'TEMPORARY') AS audit_ro_temp_before,
       has_database_privilege('avnadmin',         current_database(), 'TEMPORARY') AS avnadmin_temp_before,
       has_database_privilege('_avnadmin_monitor', current_database(), 'TEMPORARY') AS monitor_temp_before,
       has_database_privilege('public',           current_database(), 'TEMPORARY') AS public_temp_before;

SELECT datacl::text AS datacl_before FROM pg_database WHERE datname = current_database();

\echo '=== 2. CHANGE (single ACL bit; conditional no-op safeguard) ==='
BEGIN;

-- (a) Safeguard, conditional: never touch the ACL when avnadmin already has it.
DO $g5_grant$
BEGIN
    IF NOT has_database_privilege('avnadmin', current_database(), 'TEMPORARY') THEN
        EXECUTE format('GRANT TEMPORARY ON DATABASE %I TO avnadmin', current_database());
        RAISE NOTICE 'G5: issued GRANT TEMPORARY to avnadmin (was missing)';
    ELSE
        RAISE NOTICE 'G5: avnadmin already holds TEMPORARY explicitly - no GRANT issued';
    END IF;
END
$g5_grant$;

-- (b) The actual fix. A per-role DENY does not exist in PostgreSQL, so revoking
-- from audit_ro alone would be a no-op: the privilege comes from PUBLIC.
REVOKE TEMPORARY ON DATABASE defaultdb FROM PUBLIC;

\echo '=== 3. POST-CONDITIONS, ASSERTED INSIDE THE TRANSACTION (violation = rollback) ==='
DO $g5_assert$
BEGIN
    IF has_database_privilege('audit_ro', current_database(), 'TEMPORARY') THEN
        RAISE EXCEPTION 'G5: audit_ro still has TEMPORARY after REVOKE';
    END IF;
    IF NOT has_database_privilege('avnadmin', current_database(), 'TEMPORARY') THEN
        RAISE EXCEPTION 'G5: avnadmin lost TEMPORARY - application role would break';
    END IF;
    IF NOT has_database_privilege('_avnadmin_monitor', current_database(), 'TEMPORARY') THEN
        RAISE EXCEPTION 'G5: _avnadmin_monitor lost TEMPORARY - provider role would break';
    END IF;
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

\echo '=== 4. AFTER (expected: audit_ro=f, avnadmin=t, monitor=t, public_temp=f, public_connect=t) ==='
SELECT has_database_privilege('audit_ro',         current_database(), 'TEMPORARY') AS audit_ro_temp_after,
       has_database_privilege('avnadmin',         current_database(), 'TEMPORARY') AS avnadmin_temp_after,
       has_database_privilege('_avnadmin_monitor', current_database(), 'TEMPORARY') AS monitor_temp_after,
       has_database_privilege('public',           current_database(), 'TEMPORARY') AS public_temp_after,
       has_database_privilege('public',           current_database(), 'CONNECT')   AS public_connect_after;

\echo '=== 5. AFTER datacl (expected exactly: {=c/avnadmin,avnadmin=CTc/avnadmin,_avnadmin_monitor=Tc/avnadmin}) ==='
SELECT datacl::text AS datacl_after FROM pg_database WHERE datname = current_database();

\echo '=== END (only PUBLIC.TEMPORARY changed; now re-run RUN_G5_REAUDIT_fix4.ps1 to re-audit) ==='
