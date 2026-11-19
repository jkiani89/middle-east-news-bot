-- =====================================================================
-- G5 read-only permission audit (audit_ro) - Aiven PostgreSQL 18.6
-- Run as audit_ro. Read-only: no writes, no DDL, no SET ROLE.
-- Usage (Windows PowerShell):
--   $env:PGPASSWORD = (Read-Host "audit_ro password" -AsSecureString | ...)
--   psql "host=pg-2f0e4f1c-newsbot-db.e.aivencloud.com port=27362 dbname=defaultdb user=audit_ro sslmode=verify-full sslrootcert=C:/g5/aiven-ca.pem" -v ON_ERROR_STOP=1 -f C:\g5\audit_perms.sql
-- Output: capture with  2>&1 | Tee-Object -FilePath C:\g5\audit_out.txt
-- =====================================================================

\echo '=== 0. SERVER VERSION ==='
SELECT version();

\echo '=== 1. CURRENT IDENTITY / DATABASE ==='
SELECT current_user AS current_user, session_user AS session_user,
       current_database() AS database, current_schema() AS schema;

\echo '=== 2. DATABASE ACL (datacl) ==='
SELECT datname, pg_get_userbyid(datdba) AS owner, datacl
FROM pg_database
WHERE datname = current_database();

\echo '=== 3. audit_ro ROLE ATTRIBUTES ==='
SELECT rolname, rolsuper, rolcreatedb, rolcreaterole, rolbypassrls,
       rolcanlogin, rolreplication, rolconnlimit
FROM pg_roles
WHERE rolname IN ('audit_ro','avnadmin','PUBLIC')
ORDER BY rolname;

\echo '=== 4. DATABASE-LEVEL PRIVILEGES FOR audit_ro ==='
SELECT
  has_database_privilege('audit_ro', current_database(), 'CONNECT')    AS connect,
  has_database_privilege('audit_ro', current_database(), 'CREATE')     AS create_db,
  has_database_privilege('audit_ro', current_database(), 'TEMPORARY')  AS temporary,
  has_database_privilege('audit_ro', current_database(), 'TEMP')       AS temp_alias;

\echo '=== 5. PUBLIC DATABASE-LEVEL PRIVILEGES (defaults) ==='
SELECT
  has_database_privilege('public', current_database(), 'CONNECT')    AS public_connect,
  has_database_privilege('public', current_database(), 'CREATE')     AS public_create,
  has_database_privilege('public', current_database(), 'TEMPORARY')  AS public_temporary;

\echo '=== 6. SCHEMA-LEVEL PRIVILEGES (non-system) ==='
SELECT n.nspname AS schema_name,
       pg_get_userbyid(n.nspowner) AS owner,
       has_schema_privilege('audit_ro', n.oid, 'USAGE')  AS audit_ro_usage,
       has_schema_privilege('audit_ro', n.oid, 'CREATE') AS audit_ro_create,
       (SELECT n2.nspacl FROM pg_namespace n2 WHERE n2.oid = n.oid) AS nspacl
FROM pg_namespace n
WHERE n.nspname NOT LIKE 'pg_%'
  AND n.nspname <> 'information_schema'
ORDER BY 1;

\echo '=== 7. SECURITY DEFINER FUNCTIONS EXECUTABLE BY audit_ro (non-system) ==='
SELECT n.nspname AS schema_name, p.proname AS func_name,
       pg_get_function_identity_arguments(p.oid) AS args,
       p.prosecdef AS security_definer,
       has_function_privilege('audit_ro', p.oid, 'EXECUTE') AS audit_ro_execute
FROM pg_proc p
JOIN pg_namespace n ON n.oid = p.pronamespace
WHERE p.prosecdef = true
  AND n.nspname NOT IN ('pg_catalog','information_schema')
ORDER BY 1,2;

\echo '=== 8. TABLE-LEVEL WRITE PRIVILEGES FOR audit_ro (non-system) ==='
SELECT n.nspname AS schema_name, c.relname AS table_name, c.relkind,
       has_table_privilege('audit_ro', c.oid, 'SELECT')   AS sel,
       has_table_privilege('audit_ro', c.oid, 'INSERT')   AS ins,
       has_table_privilege('audit_ro', c.oid, 'UPDATE')   AS upd,
       has_table_privilege('audit_ro', c.oid, 'DELETE')   AS del,
       has_table_privilege('audit_ro', c.oid, 'TRUNCATE') AS trunc
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind IN ('r','p','v','m','f')
  AND n.nspname NOT LIKE 'pg_%'
  AND n.nspname <> 'information_schema'
  AND (has_table_privilege('audit_ro', c.oid, 'INSERT')
    OR has_table_privilege('audit_ro', c.oid, 'UPDATE')
    OR has_table_privilege('audit_ro', c.oid, 'DELETE')
    OR has_table_privilege('audit_ro', c.oid, 'TRUNCATE'))
ORDER BY 1,2;

\echo '=== 9. ROLE MEMBERSHIP OF audit_ro ==='
SELECT r.rolname AS granted_role, m.admin_option, m.inherit_option, m.set_option
FROM pg_auth_members m
JOIN pg_roles r ON r.oid = m.roleid
WHERE m.member = (SELECT oid FROM pg_roles WHERE rolname = 'audit_ro')
ORDER BY 1;

\echo '=== END OF AUDIT (read-only, no data changed) ==='
