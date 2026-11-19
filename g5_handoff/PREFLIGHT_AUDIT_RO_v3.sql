-- =====================================================================
-- G5 - PRE-FLIGHT INVENTORY for finding 3-4  (v3, READ-ONLY, server-enforced)
-- =====================================================================
-- Purpose: prove, BEFORE any ACL change, exactly which roles depend on
-- PostgreSQL's default TEMPORARY grant to PUBLIC on this database - including
-- roles that depend on it only through role membership / SET ROLE.
--
-- READ-ONLY GUARANTEE (two independent locks):
--   1. this file opens with  BEGIN TRANSACTION READ ONLY;  and ends with ROLLBACK;
--      PostgreSQL itself rejects every CREATE/ALTER/DROP/GRANT/REVOKE/COMMENT
--      and every DML inside a read-only transaction (SQLSTATE 25006).
--   2. every statement below is a SELECT; there is no write statement to begin
--      with. No temp table is created, nothing is persisted.
-- If any statement fails, psql exits (ON_ERROR_STOP) and the transaction dies
-- with the connection: still nothing changed.
--
-- Requires PostgreSQL 14+ (pg_stat_ssl, aclexplode). Verified on 16.2 locally
-- and written against the 18.6 production server.
--
-- Run (verify-full, all connections):
--   psql "host=pg-2f0e4f1c-newsbot-db.e.aivencloud.com port=27362 dbname=defaultdb user=avnadmin sslmode=verify-full sslrootcert=C:/g5_fix4/aiven-ca.pem" -v ON_ERROR_STOP=1 -f PREFLIGHT_AUDIT_RO_v3.sql
-- If libpq rejects Aiven's certificate (recorded defect: malformed IP SAN,
-- "certificate contains IP address with invalid length 16"), use the runner:
--   python g5_admin.py --mode preflight --sql PREFLIGHT_AUDIT_RO_v3.sql --ca aiven-ca.pem --user avnadmin
-- =====================================================================

\echo '=== 0. CONTEXT, GUARDS AND READ-ONLY PROOF ==='
BEGIN TRANSACTION READ ONLY;

SELECT current_user                                  AS run_as,
       session_user                                  AS session_user,
       current_database()                            AS db,
       current_setting('server_version')             AS server_version,
       current_setting('transaction_read_only')      AS tx_read_only,
       pg_is_in_recovery()                           AS in_recovery,
       (SELECT ssl     FROM pg_stat_ssl WHERE pid = pg_backend_pid()) AS ssl,
       (SELECT version FROM pg_stat_ssl WHERE pid = pg_backend_pid()) AS tls_version,
       pg_get_userbyid((SELECT datdba FROM pg_database WHERE datname = current_database())) AS db_owner;

\echo '=== 1. DATABASE ACL AND OWNER ==='
SELECT d.datname,
       pg_get_userbyid(d.datdba) AS owner,
       d.datacl,
       has_database_privilege('public', d.datname, 'TEMPORARY') AS public_temp,
       has_database_privilege('public', d.datname, 'CONNECT')   AS public_connect,
       has_database_privilege('public', d.datname, 'CREATE')    AS public_create
FROM pg_database d
WHERE d.datname = current_database();

\echo '=== 2. THE PUBLIC ENTRY, EXPLODED (grantee 0 = PUBLIC) ==='
SELECT x.grantee, x.privilege_type, x.is_grantable,
       pg_get_userbyid(x.grantor) AS granted_by
FROM pg_database d CROSS JOIN LATERAL aclexplode(d.datacl) AS x
WHERE d.datname = current_database() AND x.grantee = 0
ORDER BY x.privilege_type;

\echo '=== 3. EVERY ROLE: EFFECTIVE vs EXPLICIT TEMPORARY ==='
-- effective_temp comes from explicit grants + role membership + PUBLIC (superusers/owner always true).
-- direct_temp is true only when a datacl entry names that role.
WITH d AS (SELECT datname, datacl FROM pg_database WHERE datname = current_database()),
direct AS (
    SELECT x.grantee AS role_oid, bool_or(x.privilege_type = 'TEMPORARY') AS direct_temp
    FROM d CROSS JOIN LATERAL aclexplode(d.datacl) AS x
    WHERE x.grantee <> 0
    GROUP BY x.grantee
)
SELECT r.rolname,
       r.rolcanlogin,
       r.rolsuper,
       r.rolreplication,
       has_database_privilege(r.oid, (SELECT datname FROM d), 'TEMPORARY') AS effective_temp,
       COALESCE(dd.direct_temp, false) AS direct_temp
FROM pg_roles r
LEFT JOIN direct dd ON dd.role_oid = r.oid
ORDER BY r.rolcanlogin DESC, r.rolname;

\echo '=== 3b. MEMBERSHIP EDGES OF LOGIN ROLES (USAGE = inherited now, SET = usable via SET ROLE) ==='
-- Verified answer to "are memberships and SET ROLE covered?": yes, and both
-- directions are shown. pg_has_role() resolves nested membership transitively.
SELECT m.member::regrole::text  AS member_role,
       m.roleid::regrole::text  AS granted_role,
       m.admin_option,
       pg_has_role(m.member, m.roleid, 'USAGE') AS inherits_now,
       pg_has_role(m.member, m.roleid, 'SET')   AS can_set_role,
       has_database_privilege(m.roleid, current_database(), 'TEMPORARY') AS granted_role_has_temp
FROM pg_auth_members m
JOIN pg_roles r ON r.oid = m.member
WHERE r.rolcanlogin
ORDER BY 1, 2;

\echo '=== 3c. CAPABILITY PER LOGIN ROLE AFTER THE REVOKE (the decision table) ==='
-- KEEPS (explicit grant) / KEEPS (via SET ROLE membership) / KEEPS (superuser|owner)
--   -> unaffected by REVOKE TEMPORARY ... FROM PUBLIC
-- LOSES -> these are the roles the change actually affects.
WITH explicit_temp AS (
    SELECT x.grantee AS role_oid
    FROM pg_database d CROSS JOIN LATERAL aclexplode(d.datacl) AS x
    WHERE d.datname = current_database()
      AND x.privilege_type = 'TEMPORARY'
      AND x.grantee <> 0
)
SELECT r.rolname,
       has_database_privilege(r.oid, current_database(), 'TEMPORARY') AS temp_now,
       (r.oid IN (SELECT role_oid FROM explicit_temp))                 AS temp_explicit,
       (SELECT COALESCE(string_agg(e.role_oid::regrole::text, ', ' ORDER BY e.role_oid::regrole::text), '')
          FROM explicit_temp e
         WHERE e.role_oid <> r.oid
           AND pg_has_role(r.oid, e.role_oid, 'SET'))                 AS keeps_via_set_role,
       CASE
         WHEN r.rolsuper THEN 'KEEPS (superuser)'
         WHEN pg_get_userbyid((SELECT datdba FROM pg_database WHERE datname = current_database())) = r.rolname
              THEN 'KEEPS (owner)'
         WHEN r.oid IN (SELECT role_oid FROM explicit_temp) THEN 'KEEPS (explicit grant)'
         WHEN EXISTS (SELECT 1 FROM explicit_temp e WHERE e.role_oid <> r.oid AND pg_has_role(r.oid, e.role_oid, 'SET'))
              THEN 'KEEPS (via SET ROLE membership)'
         WHEN has_database_privilege(r.oid, current_database(), 'TEMPORARY') THEN 'LOSES'
         ELSE 'NO_TEMP'
       END AS impact_after_revoke
FROM pg_roles r
WHERE r.rolcanlogin
ORDER BY 5, 1;

\echo '=== 4. SUMMARY (STOP-CRITERION INPUTS) ==='
WITH explicit_temp AS (
    SELECT x.grantee AS role_oid
    FROM pg_database d CROSS JOIN LATERAL aclexplode(d.datacl) AS x
    WHERE d.datname = current_database() AND x.privilege_type = 'TEMPORARY' AND x.grantee <> 0
),
owner_oid AS (SELECT datdba FROM pg_database WHERE datname = current_database())
SELECT count(*) FILTER (
           WHERE r.rolcanlogin
             AND NOT r.rolsuper
             AND r.oid <> (SELECT datdba FROM owner_oid)
             AND r.oid NOT IN (SELECT role_oid FROM explicit_temp)
             AND NOT EXISTS (SELECT 1 FROM explicit_temp e WHERE e.role_oid <> r.oid AND pg_has_role(r.oid, e.role_oid, 'SET'))
             AND has_database_privilege(r.oid, current_database(), 'TEMPORARY')
       ) AS login_roles_that_lose_temp,
       count(*) FILTER (
           WHERE r.rolcanlogin
             AND has_database_privilege(r.oid, current_database(), 'TEMPORARY')
             AND NOT (r.oid IN (SELECT role_oid FROM explicit_temp))
             AND NOT r.rolsuper
             AND r.oid <> (SELECT datdba FROM owner_oid)
       ) AS login_roles_temp_only_from_public_or_membership,
       count(*) FILTER (WHERE r.rolcanlogin) AS total_login_roles,
       count(*) FILTER (WHERE NOT r.rolcanlogin AND r.oid < 16384) AS predefined_nologin_roles_ignored
FROM pg_roles r;

\echo '=== 5. TEMP RELATIONS PRESENT RIGHT NOW (session impact signal; 0 = nobody is mid-temp-work) ==='
SELECT count(*)                      AS temp_relations,
       count(DISTINCT n.nspname)     AS temp_schemas
FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE c.relpersistence = 't';

\echo '=== 6. FULL ACL BASELINE (diff this against the AFTER snapshot) ==='
SELECT current_database() AS db,
       d.datacl::text     AS datacl_baseline,
       COALESCE(CASE WHEN x.grantee = 0 THEN 'PUBLIC' ELSE pg_get_userbyid(x.grantee) END, '-') AS grantee,
       COALESCE(x.privilege_type, '-') AS privilege_type,
       COALESCE(x.is_grantable, false) AS is_grantable,
       COALESCE(pg_get_userbyid(x.grantor), '-') AS granted_by
FROM pg_database d
LEFT JOIN LATERAL aclexplode(d.datacl) AS x ON true
WHERE d.datname = current_database()
ORDER BY 3, 4;

\echo '=== 7. DOES ANY SERVICE NEED TEMP? (code-level answer: NO - see docs/G5_AUDIT.md) ==='
-- Evidence gathered 2026-11-19 over the whole repository (app/, alembic/,
-- scripts/, .github/, docs/):
--   * zero occurrences of CREATE TEMP TABLE / ON COMMIT DROP / pg_temp usage;
--   * every SQL for the app runs through SQLAlchemy/Alembic DDL+DML only;
--   * zero GRANT/REVOKE/ALTER ROLE inside the repository at all;
--   * the only DB role referenced anywhere is avnadmin (owner, explicit TEMP),
--     which is what DATABASE_URL uses for app, migrations, backfill and CI.
-- Therefore no application, migration, backfill or CI job requires TEMPORARY
-- on this database. Section 3c proves it per role for the live cluster.

\echo '=== END OF PREFLIGHT (read-only; transaction rolled back) ==='
ROLLBACK;
