-- =====================================================================
-- MANAGEMENT SQL - run as avnadmin ONLY
-- Prerequisite: G5 probe returned FAIL on 3-4 (audit_ro has TEMPORARY on defaultdb).
-- Reason: PostgreSQL grants TEMPORARY to PUBLIC by default on every database.
-- This is the ONLY way to make 3-4 PASS without granting/revoking CONNECT.
-- Review evidence first; do not run blindly.
-- =====================================================================

\echo '=== BEFORE: audit_ro TEMPORARY ==='
SELECT has_database_privilege('audit_ro', current_database(), 'TEMPORARY') AS audit_ro_temp_before;

-- Preserve application capability: app connects as avnadmin (superuser-equivalent on Aiven),
-- but grant explicitly so revoking from PUBLIC cannot remove app temp access.
GRANT TEMPORARY ON DATABASE defaultdb TO avnadmin;

-- The actual fix:
REVOKE TEMPORARY ON DATABASE defaultdb FROM PUBLIC;

\echo '=== AFTER: audit_ro TEMPORARY (must be false) ==='
SELECT has_database_privilege('audit_ro', current_database(), 'TEMPORARY') AS audit_ro_temp_after;

\echo '=== AFTER: avnadmin TEMPORARY (must be true) ==='
SELECT has_database_privilege('avnadmin', current_database(), 'TEMPORARY') AS avnadmin_temp_after;

\echo '=== END (no tables/schemas/data changed) ==='
