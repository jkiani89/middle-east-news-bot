-- =====================================================================
-- G5 - MANAGEMENT SQL for finding 3-4 (run as avnadmin ONLY, after approval)
-- =====================================================================
-- Finding: audit_ro holds TEMPORARY on defaultdb through PostgreSQL's default
-- grant to PUBLIC (datacl entry "=Tc/avnadmin"). The probe criterion 3-4
-- requires can_temp = false for a read-only auditor, so it fails until the
-- database ACL is changed. This is a real deviation, not a criterion defect.
--
-- Scope of the change: database-level ACL only. No table, schema, data,
-- function or ownership change. No session, no role attribute.
--
-- Impact (documented, verify with PREFLIGHT_AUDIT_RO_v2.sql first):
--   * audit_ro loses TEMPORARY  -> 3-4 can pass.
--   * avnadmin keeps TEMPORARY through its explicit "avnadmin=CTc" entry;
--     the GRANT below is a no-op safeguard in case that entry is ever removed.
--   * _avnadmin_monitor keeps TEMPORARY through its explicit "=Tc" entry.
--   * Any other role without an explicit grant would also lose TEMPORARY,
--     including roles created in the future on this database.
--   * The application does not use temp tables (verified by repository search),
--     and connects as avnadmin, so it is not affected.
--
-- Run:
--   $env:PGPASSWORD = (Read-Host "avnadmin password" -AsSecureString | ...)
--   psql "host=pg-2f0e4f1c-newsbot-db.e.aivencloud.com port=27362 dbname=defaultdb user=avnadmin sslmode=require" -v ON_ERROR_STOP=1 -f FIX_AUDIT_RO_v2.sql
--   Remove-Item Env:PGPASSWORD
-- =====================================================================

\echo '=== BEFORE: audit_ro TEMPORARY (expected: true) ==='
SELECT has_database_privilege('audit_ro', current_database(), 'TEMPORARY') AS audit_ro_temp_before;

\echo '=== BEFORE: avnadmin TEMPORARY (expected: true) ==='
SELECT has_database_privilege('avnadmin', current_database(), 'TEMPORARY') AS avnadmin_temp_before;

\echo '=== BEFORE: datacl ==='
SELECT datacl FROM pg_database WHERE datname = current_database();

-- Safeguard: the application role must keep TEMPORARY explicitly, so that the
-- PUBLIC revoke cannot remove it (it is already explicit on this cluster).
GRANT TEMPORARY ON DATABASE defaultdb TO avnadmin;

-- The actual fix: remove PostgreSQL's default TEMPORARY grant from PUBLIC.
-- A per-role DENY does not exist in PostgreSQL, so revoking from audit_ro alone
-- would be a no-op: audit_ro inherits the privilege from PUBLIC.
REVOKE TEMPORARY ON DATABASE defaultdb FROM PUBLIC;

\echo '=== AFTER: audit_ro TEMPORARY (must be false) ==='
SELECT has_database_privilege('audit_ro', current_database(), 'TEMPORARY') AS audit_ro_temp_after;

\echo '=== AFTER: avnadmin TEMPORARY (must be true) ==='
SELECT has_database_privilege('avnadmin', current_database(), 'TEMPORARY') AS avnadmin_temp_after;

\echo '=== AFTER: _avnadmin_monitor TEMPORARY (must be true) ==='
SELECT has_database_privilege('_avnadmin_monitor', current_database(), 'TEMPORARY') AS monitor_temp_after;

\echo '=== AFTER: datacl ==='
SELECT datacl FROM pg_database WHERE datname = current_database();

\echo '=== END (no tables/schemas/data/roles changed; re-run the G5 probe now) ==='
