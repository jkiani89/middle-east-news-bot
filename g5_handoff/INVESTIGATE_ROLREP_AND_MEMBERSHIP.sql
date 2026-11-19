-- =====================================================================
-- G5 - INDEPENDENT INVESTIGATION: audit_ro.rolreplication + avnadmin -> audit_ro membership
-- =====================================================================
-- Purpose: read-only, server-enforced investigation of two items that were
-- documented as "optional / out of scope" during the G5 audit closure:
--   (A) audit_ro.rolreplication = true  (replication capability on a read-only audit role)
--   (B) avnadmin is a MEMBER of audit_ro (admin_option=true, inherits_now=true,
--       can_set_role=true) - an unexpected membership edge discovered in preflight 3b.
--
-- CONSTRAINTS:
--   - READ-ONLY: opens with BEGIN TRANSACTION READ ONLY, ends with ROLLBACK.
--   - No GRANT/REVOKE/ALTER/CREATE/DROP/COMMENT/SET ROLE/DDL/DML.
--   - No production changes of any kind.
--   - All connections verify-full (use g5_admin.py runner if libpq rejects Aiven cert).
--
-- Run (verify-full):
--   psql "host=pg-2f0e4f1c-newsbot-db.e.aivencloud.com port=27362 dbname=defaultdb user=avnadmin sslmode=verify-full sslrootcert=C:/g5_final_v4/aiven-ca.pem" -v ON_ERROR_STOP=1 -f INVESTIGATE_ROLREP_AND_MEMBERSHIP.sql
-- Or via runner:
--   python g5_admin.py --mode preflight --sql INVESTIGATE_ROLREP_AND_MEMBERSHIP.sql --ca aiven-ca.pem --user avnadmin
-- =====================================================================

\echo '=== 0. CONTEXT (read-only proof) ==='
BEGIN TRANSACTION READ ONLY;

SELECT current_user AS run_as,
       session_user AS session_user,
       current_database() AS db,
       current_setting('server_version') AS server_version,
       current_setting('transaction_read_only') AS tx_read_only,
       pg_is_in_recovery() AS in_recovery,
       (SELECT ssl FROM pg_stat_ssl WHERE pid = pg_backend_pid()) AS ssl,
       (SELECT version FROM pg_stat_ssl WHERE pid = pg_backend_pid()) AS tls_version;

-- =====================================================================
-- PART A: audit_ro.rolreplication
-- =====================================================================

\echo '=== A1. audit_ro ROLE ATTRIBUTES (full picture) ==='
SELECT r.rolname,
       r.rolsuper,
       r.rolinherit,
       r.rolcreaterole,
       r.rolcreatedb,
       r.rolcanlogin,
       r.rolreplication,
       r.rolbypassrls,
       r.rolconnlimit,
       r.rolvaliduntil,
       r.rolpassword IS NOT NULL AS has_password
FROM pg_roles r
WHERE r.rolname = 'audit_ro';

\echo '=== A2. WHAT rolreplication ENABLES - replication slots owned by audit_ro ==='
SELECT s.slot_name,
       s.plugin,
       s.slot_type,
       s.datoid,
       s.database,
       s.active,
       s.active_pid,
       s.xmin,
       s.catalog_xmin,
       s.restart_lsn,
       s.confirmed_flush_lsn,
       s.wal_status,
       s.safe_wal_size
FROM pg_replication_slots s
WHERE s.slot_name IS NOT NULL
  AND (pg_get_userbyid(s.datoid) = 'audit_ro' OR true)
ORDER BY s.slot_name;
-- Note: pg_replication_slots has no owner column; slots are server-level.
-- We list ALL slots so the human can see if any exist at all.

\echo '=== A3. REPLICATION-RELATED PRIVILEGES (does audit_ro have any?) ==='
-- Check if audit_ro has privileges on replication-related objects.
SELECT has_table_privilege('audit_ro', 'pg_replication_slots', 'SELECT') AS repl_slots_select,
       has_function_privilege('audit_ro', 'pg_create_physical_replication_slot(text)', 'EXECUTE') AS create_phys_slot,
       has_function_privilege('audit_ro', 'pg_create_logical_replication_slot(text,text)', 'EXECUTE') AS create_log_slot,
       has_function_privilege('audit_ro', 'pg_drop_replication_slot(text)', 'EXECUTE') AS drop_slot,
       has_function_privilege('audit_ro', 'pg_replication_slot_advance(text,pg_lsn)', 'EXECUTE') AS advance_slot;

\echo '=== A4. REPLICATION SLOTS SUMMARY (server-wide, for context) ==='
SELECT count(*) AS total_slots,
       count(*) FILTER (WHERE active) AS active_slots,
       count(*) FILTER (WHERE slot_type = 'logical') AS logical_slots,
       count(*) FILTER (WHERE slot_type = 'physical') AS physical_slots
FROM pg_replication_slots;

\echo '=== A5. PUBLICATIONS (logical replication sources) ==='
SELECT pubname, pubowner::regrole AS owner, puballtables, pubinsert, pubupdate, pubdelete, pubtruncate
FROM pg_publication
ORDER BY pubname;

\echo '=== A6. SUBSCRIPTIONS (logical replication targets) ==='
SELECT subname, subowner::regrole AS owner, subenabled, subconninfo IS NOT NULL AS has_conninfo
FROM pg_subscription
ORDER BY subname;

-- =====================================================================
-- PART B: avnadmin membership in audit_ro
-- =====================================================================

\echo '=== B1. THE MEMBERSHIP EDGE (avnadmin -> audit_ro) ==='
SELECT m.member::regrole::text AS member_role,
       m.roleid::regrole::text AS granted_role,
       m.admin_option,
       m.inherit_option,
       m.set_option,
       pg_has_role(m.member, m.roleid, 'USAGE') AS inherits_now,
       pg_has_role(m.member, m.roleid, 'SET') AS can_set_role,
       has_database_privilege(m.roleid, current_database(), 'TEMPORARY') AS granted_role_has_temp,
       has_database_privilege(m.member, current_database(), 'TEMPORARY') AS member_has_temp
FROM pg_auth_members m
WHERE m.member::regrole::text = 'avnadmin'
  AND m.roleid::regrole::text = 'audit_ro';

\echo '=== B2. ALL MEMBERSHIP EDGES FOR audit_ro (who is a member?) ==='
SELECT m.member::regrole::text AS member_role,
       m.admin_option,
       m.inherit_option,
       m.set_option,
       pg_has_role(m.member, m.roleid, 'USAGE') AS inherits_now,
       pg_has_role(m.member, m.roleid, 'SET') AS can_set_role
FROM pg_auth_members m
WHERE m.roleid::regrole::text = 'audit_ro'
ORDER BY 1;

\echo '=== B3. ALL MEMBERSHIP EDGES FOR avnadmin (what is avnadmin a member of?) ==='
SELECT m.roleid::regrole::text AS granted_role,
       m.admin_option,
       m.inherit_option,
       m.set_option,
       pg_has_role(m.member, m.roleid, 'USAGE') AS inherits_now,
       pg_has_role(m.member, m.roleid, 'SET') AS can_set_role
FROM pg_auth_members m
WHERE m.member::regrole::text = 'avnadmin'
ORDER BY 1;

\echo '=== B4. WHAT audit_ro GRANTS THAT avnadmin WOULD INHERIT ==='
-- Effective privileges avnadmin gains through audit_ro membership.
SELECT has_database_privilege('avnadmin', current_database(), 'CONNECT') AS db_connect,
       has_database_privilege('avnadmin', current_database(), 'CREATE') AS db_create,
       has_database_privilege('avnadmin', current_database(), 'TEMPORARY') AS db_temp,
       has_database_privilege('audit_ro', current_database(), 'CONNECT') AS audit_connect,
       has_database_privilege('audit_ro', current_database(), 'CREATE') AS audit_create,
       has_database_privilege('audit_ro', current_database(), 'TEMPORARY') AS audit_temp;

\echo '=== B5. SCHEMA-LEVEL: what does audit_ro have that avnadmin inherits? ==='
SELECT n.nspname AS schema_name,
       has_schema_privilege('audit_ro', n.oid, 'USAGE') AS audit_usage,
       has_schema_privilege('audit_ro', n.oid, 'CREATE') AS audit_create,
       has_schema_privilege('avnadmin', n.oid, 'USAGE') AS avnadmin_usage,
       has_schema_privilege('avnadmin', n.oid, 'CREATE') AS avnadmin_create
FROM pg_namespace n
WHERE n.nspname NOT LIKE 'pg_%'
  AND n.nspname <> 'information_schema'
ORDER BY n.nspname;

\echo '=== B6. TABLE-LEVEL: does audit_ro have SELECT on any user table that avnadmin inherits? ==='
SELECT t.schemaname,
       t.tablename,
       has_table_privilege('audit_ro', t.schemaname || '.' || t.tablename, 'SELECT') AS audit_select,
       has_table_privilege('avnadmin', t.schemaname || '.' || t.tablename, 'SELECT') AS avnadmin_select
FROM pg_tables t
WHERE t.schemaname NOT IN ('pg_catalog', 'information_schema')
ORDER BY t.schemaname, t.tablename;

-- =====================================================================
-- PART C: NECESSITY ASSESSMENT (codebase-derived, documented here)
-- =====================================================================

\echo '=== C1. audit_ro IS A MEMBER OF WHAT? (reverse lookup) ==='
-- If audit_ro is a member of any role, it inherits that role's privileges.
SELECT m.roleid::regrole::text AS granted_role,
       m.admin_option,
       m.inherit_option,
       m.set_option
FROM pg_auth_members m
WHERE m.member::regrole::text = 'audit_ro'
ORDER BY 1;

\echo '=== C2. audit_ro OWNED OBJECTS (does it own anything?) ==='
SELECT c.relname, c.relkind, n.nspname
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE pg_get_userbyid(c.relowner) = 'audit_ro'
ORDER BY n.nspname, c.relname;

\echo '=== C3. audit_ro DEFAULT PRIVILEGES (any future grants?) ==='
SELECT pg_get_userbyid(d.defaclrole) AS role,
       n.nspname AS schema,
       d.defaclobjtype AS obj_type,
       d.defaclacl AS acl
FROM pg_default_acl d
LEFT JOIN pg_namespace n ON n.oid = d.defaclnamespace
WHERE pg_get_userbyid(d.defaclrole) = 'audit_ro';

\echo '=== END OF INVESTIGATION (read-only; transaction rolled back) ==='
ROLLBACK;
EOF
echo "created"