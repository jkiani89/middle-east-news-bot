-- =====================================================================
-- G5 - PRE-FLIGHT INVENTORY for the 3-4 finding (READ-ONLY)
-- =====================================================================
-- Purpose: document, BEFORE any ACL change, exactly which roles rely on
-- PostgreSQL's default TEMPORARY grant to PUBLIC on this database.
--
-- Safety: every statement below is a SELECT. No GRANT / REVOKE / ALTER,
--         no writes, no DDL. Safe to run as audit_ro or avnadmin.
--
-- Why TEMPORARY exists at all: PostgreSQL's stock database ACL gives PUBLIC
-- CONNECT and TEMPORARY. Aiven materialised datacl on defaultdb, but PUBLIC's
-- default entry was preserved, so audit_ro inherits TEMPORARY without any
-- direct grant of its own. This file proves that per role.
--
-- Run:
--   $env:PGPASSWORD = (Read-Host "password" -AsSecureString | ...)
--   psql "host=pg-2f0e4f1c-newsbot-db.e.aivencloud.com port=27362 dbname=defaultdb user=avnadmin sslmode=require" -v ON_ERROR_STOP=1 -f PREFLIGHT_AUDIT_RO_v2.sql
-- =====================================================================

\echo '=== 0. CONTEXT ==='
SELECT current_user AS run_as, current_database() AS db,
       current_setting('server_version') AS server_version;

\echo '=== 1. DATABASE ACL AND OWNER ==='
SELECT d.datname, pg_get_userbyid(d.datdba) AS owner, d.datacl,
       has_database_privilege('public', d.datname, 'TEMPORARY')  AS public_temp,
       has_database_privilege('public', d.datname, 'CONNECT')    AS public_connect,
       has_database_privilege('public', d.datname, 'CREATE')     AS public_create
FROM pg_database d
WHERE d.datname = current_database();

\echo '=== 2. THE PUBLIC ENTRY, EXPLODED (grantee 0 = PUBLIC) ==='
SELECT x.grantee, x.privilege_type, x.is_grantable, x.grantor::regrole AS granted_by
FROM pg_database d CROSS JOIN LATERAL aclexplode(d.datacl) AS x
WHERE d.datname = current_database() AND x.grantee = 0
ORDER BY x.privilege_type;

\echo '=== 3. EVERY ROLE: EFFECTIVE vs DIRECT TEMPORARY ==='
-- effective_temp comes from direct grants + role membership + PUBLIC.
-- direct_temp is set only when a grantee entry for that role exists in datacl.
WITH d AS (SELECT datname, datacl FROM pg_database WHERE datname = current_database()),
direct AS (
    SELECT p.rolname, bool_or(x.privilege_type = 'TEMPORARY') AS direct_temp
    FROM d CROSS JOIN LATERAL aclexplode(d.datacl) AS x
    JOIN pg_roles p ON p.oid = x.grantee
    GROUP BY p.rolname
)
SELECT r.rolname,
       r.rolcanlogin,
       r.rolsuper,
       r.rolreplication,
       has_database_privilege(r.oid, (SELECT datname FROM d), 'TEMPORARY') AS effective_temp,
       COALESCE(dd.direct_temp, false) AS direct_temp,
       CASE
         -- Predefined PostgreSQL roles (NOLOGIN, oid < FirstNormalObjectId) are
         -- listed for completeness but cannot log in, so their TEMPORARY
         -- capability is irrelevant to this decision.
         WHEN NOT r.rolcanlogin AND r.oid < 16384 THEN 'PREDEFINED_BUILTIN'
         WHEN has_database_privilege(r.oid, (SELECT datname FROM d), 'TEMPORARY')
              AND NOT COALESCE(dd.direct_temp, false)
              AND NOT r.rolsuper
              AND pg_get_userbyid((SELECT datdba FROM pg_database WHERE datname = current_database())) <> r.rolname
           THEN 'WOULD_LOSE_TEMP'
         WHEN has_database_privilege(r.oid, (SELECT datname FROM d), 'TEMPORARY')
           THEN 'KEEPS_TEMP'
         ELSE 'NO_TEMP'
       END AS impact_of_revoking_public_temp
FROM pg_roles r
LEFT JOIN direct dd ON dd.rolname = r.rolname
ORDER BY impact_of_revoking_public_temp, r.rolname;

\echo '=== 4. SUMMARY: HOW MANY ROLES WOULD LOSE TEMPORARY ==='
WITH d AS (SELECT datname, datacl FROM pg_database WHERE datname = current_database()),
direct AS (
    SELECT x.grantee AS oid, bool_or(x.privilege_type = 'TEMPORARY') AS direct_temp
    FROM d CROSS JOIN LATERAL aclexplode(d.datacl) AS x
    GROUP BY x.grantee
)
SELECT count(*) FILTER (
           WHERE r.rolcanlogin
             AND r.oid >= 16384
             AND has_database_privilege(r.oid, (SELECT datname FROM d), 'TEMPORARY')
             AND NOT COALESCE(dd.direct_temp, false)
             AND NOT r.rolsuper
             AND pg_get_userbyid((SELECT datdba FROM pg_database WHERE datname = current_database())) <> r.rolname
       ) AS login_roles_that_would_lose_temp,
       count(*) FILTER (WHERE r.rolcanlogin AND r.oid >= 16384) AS total_login_roles,
       count(*) FILTER (WHERE NOT r.rolcanlogin AND r.oid < 16384) AS predefined_roles_ignored
FROM pg_roles r
LEFT JOIN direct dd ON dd.oid = r.oid;

\echo '=== 5. TEMP RELATIONS CURRENTLY VISIBLE (impact signal) ==='
SELECT count(*) AS temp_relations_visible,
       count(DISTINCT n.nspname) AS temp_schemas
FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE c.relpersistence = 't';

\echo '=== 6. DOES THE APPLICATION USE TEMP TABLES? (code-level answer: NO) ==='
-- Repository search on 2026-11-19 found no CREATE TEMP TABLE / ON COMMIT DROP and
-- no pg_temp usage in app/, alembic/ or scripts/. The application connects as the
-- database owner (avnadmin), which keeps TEMPORARY through its explicit grant.
-- This section is informational; it queries nothing.

\echo '=== END OF PREFLIGHT (read-only, nothing changed) ==='
