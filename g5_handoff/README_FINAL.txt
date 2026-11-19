G5 FINDING 3-4 - FINAL HANDOVER PACKAGE (fix5 / SQL v4 / probe fix3)
====================================================================
Status: TOOLING COMPLETE - PRODUCTION FIX AWAITING EXPLICIT HUMAN APPROVAL.
Nothing in this package has been run against production. No GRANT, REVOKE,
ALTER ROLE or any other write has been executed on the production database.

1. WHAT THIS PACKAGE DOES
-------------------------
Finding 3-4 of the G5 audit: role audit_ro holds TEMPORARY on database
defaultdb only because of PostgreSQL's default grant to PUBLIC. A per-role DENY
does not exist in PostgreSQL, so the only fix is:

    REVOKE TEMPORARY ON DATABASE defaultdb FROM PUBLIC;

A naive revoke would also strip TEMPORARY from two Aiven-internal roles
(_aiven_repl, _aiven_rewind) that inherit it only through PUBLIC. The surgical
fix (v4) grants TEMPORARY to those two roles explicitly FIRST, then revokes
PUBLIC, so the blast radius is limited to audit_ro (the actual finding):

    GRANT TEMPORARY ON DATABASE defaultdb TO _aiven_repl;     (idempotent)
    GRANT TEMPORARY ON DATABASE defaultdb TO _aiven_rewind;    (idempotent)
    REVOKE TEMPORARY ON DATABASE defaultdb FROM PUBLIC;

avnadmin (application/migration/CI owner) and _avnadmin_monitor (Aiven) both
hold TEMPORARY explicitly and keep it. CONNECT for PUBLIC is untouched.

2. FILES AND SHA-256 (verify before running)
--------------------------------------------
a3aeb1fe75a33a6622fa30a142e7752d073115f3e182dbcbb388ac561ad0643d  g5_probe_fix3.py
8163ecbfa5dcb94c4f2d2c7532e3877103a5cb619a0d1b0aa05ad5673b09dee1  g5_admin.py
98e47b5f4652f4f3ca2b025426d05454a94ecf2f628af09eb0157dd3fb357328  RUN_G5_MAINT_fix5.ps1
fe80bc4914fad18e383c39749db8187cf8ca989b48fc3f6cdb31c788b6edf98f  RUN_G5_REAUDIT_fix5.ps1
0c27e5e37ef781df3a80072199a7bed8db701d0b8705e47dd715f1791659ec20  PREFLIGHT_AUDIT_RO_v3.sql
0c85a2e2ed1d3daedb6733d67eead2ac7e5ed3a7bbd10aed7e8ab0fab9341ce8  FIX_AUDIT_RO_v4.sql
569373ad13196b053a6f45ba407477a12d826162652f225e7cbaf591845cfc8e  audit_perms.sql
70e57f33b29936a458aad6e9cb99425f6d6d50ed1ab7770e853f590efefbcea4  aiven-ca.pem

PowerShell: Get-FileHash <file> -Algorithm SHA256

3. PREREQUISITES (Windows)
--------------------------
* Windscribe connected (the Aiven host is not reachable without it).
* Python 3.11+ with asyncpg 0.32.0:  pip install --user asyncpg==0.32.0
* No psql needed. Everything runs through g5_admin.py, which has exactly one
  TLS mode: CERT_REQUIRED + check_hostname=True + aiven-ca.pem (verify-full).
  There is no CERT_NONE and no sslmode=require code path. If the CA file is
  missing the runner refuses to connect.
* psql users may instead use, for the same two SQL files:
    psql "host=pg-2f0e4f1c-newsbot-db.e.aivencloud.com port=27362 dbname=defaultdb user=avnadmin sslmode=verify-full sslrootcert=C:/g5_final_v4/aiven-ca.pem" -v ON_ERROR_STOP=1 -f PREFLIGHT_AUDIT_RO_v3.sql
  (Windows libpq may reject Aiven's leaf certificate because of a malformed IP
  SAN: "certificate contains IP address with invalid length 16". That is a
  provider certificate defect, not an audit result; use the Python path.)

4. STEP 1 - PREFLIGHT (READ-ONLY, RUN THIS FIRST)
-------------------------------------------------
  $dest = "C:\g5_final_v4"
  New-Item -ItemType Directory -Path $dest | Out-Null
  Expand-Archive -Path "$HOME\Downloads\g5_final_v4.zip" -DestinationPath $dest   # never -Force
  Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass -Force
  & (Join-Path $dest 'RUN_G5_MAINT_fix5.ps1')

The preflight is read-only twice: the SQL file opens BEGIN TRANSACTION READ ONLY
and the runner sets default_transaction_read_only=on. The server itself refuses
any write (SQLSTATE 25006). Raw output is saved to preflight_out_<stamp>.txt.

Expected output (production):
  * "TLS: verify-full (CERT_REQUIRED + check_hostname)"
  * "guard: default_transaction_read_only=on"
  * section 1: public_temp = t, public_connect = t, public_create = f
  * section 3c: LOSES may include audit_ro, _aiven_repl, _aiven_rewind (all handled by v4)
  * section 4: login_roles_that_lose_temp = 3 (audit_ro + 2 Aiven-internal),
               total_login_roles = 7
  * section 5: temp_relations = 0
  * section 6: datacl_baseline = {=Tc/avnadmin,avnadmin=CTc/avnadmin,_avnadmin_monitor=Tc/avnadmin}

STOP (do not proceed, send the output to the owner) if any of these is true:
  * any LOGIN role OTHER than audit_ro, _aiven_repl, _aiven_rewind is classified LOSES;
  * section 3b shows a membership/SET ROLE edge that gives a fourth role TEMP;
  * temp_relations > 0 (a session is mid-work);
  * datacl_baseline differs from the value above;
  * verify-full or default_transaction_read_only=on is not reported;
  * any error, or any role/dependency the audit document does not explain.

5. STEP 2 - FIX (ONLY AFTER EXPLICIT APPROVAL)
----------------------------------------------
  & (Join-Path $dest 'RUN_G5_MAINT_fix5.ps1') -ApplyFix

The script re-runs the preflight, re-applies the stop criteria, then asks you to
type exactly:  APPLY TEMPORARY REVOKE

Inside the fix: guards (must be avnadmin / defaultdb / not in recovery /
audit_ro, _aiven_repl, _aiven_rewind all currently hold TEMP) ->
BEGIN -> conditional GRANT to avnadmin (normally a no-op) ->
GRANT TEMPORARY to _aiven_repl (if no explicit entry) ->
GRANT TEMPORARY to _aiven_rewind (if no explicit entry) ->
REVOKE TEMPORARY ... FROM PUBLIC -> in-transaction asserts -> COMMIT.
If any assert fails the transaction aborts and nothing is committed; the runner
then issues ROLLBACK and exits non-zero. Manual equivalent:

  python g5_admin.py --mode fix --apply-fix --sql FIX_AUDIT_RO_v4.sql --ca aiven-ca.pem --user avnadmin --verbose

Expected after: audit_ro=f, avnadmin=t, monitor=t, repl=t, rewind=t,
public_temp=f, public_connect=t,
datacl_after = {=c/avnadmin,avnadmin=CTc/avnadmin,_avnadmin_monitor=Tc/avnadmin,_aiven_repl=T/avnadmin,_aiven_rewind=T/avnadmin}

6. STEP 3 - RE-AUDIT (verify-full, role audit_ro)
-------------------------------------------------
  & (Join-Path $dest 'RUN_G5_REAUDIT_fix5.ps1')

Expected: probe_version fix3, 3-4 = PASS, verdict = PASS. A JSON report is
written next to the script.

7. VALIDATION ALREADY DONE (offline, NOT production)
----------------------------------------------------
* 23/23 offline runner tests pass (splitting, allow-list incl. DDL hidden in a
  DO block, TLS flags, mandatory CA, statement order).
* Full cycle on a throwaway PostgreSQL 16.2 cluster (with _aiven_repl and
  _aiven_rewind roles created to mirror production):
  preflight (3 LOSES: audit_ro, _aiven_repl, _aiven_rewind),
  server-refused write proof ("cannot execute CREATE TABLE in a read-only transaction"),
  forced rollback proof (assert failed when _avnadmin_monitor lacked TEMP, datacl unchanged),
  real fix (avnadmin no-op grant, repl+rewind granted, PUBLIC revoked),
  second preflight (0 LOSES), probe afterwards (3-4 = PASS), data intact.
* LIMITATION: production is PostgreSQL 18.6; the lab cluster is 16.2. The SQL
  uses only long-stable catalogs (pg_database, pg_roles, pg_auth_members,
  aclexplode, pg_stat_ssl, pg_class.relpersistence, has_database_privilege) and
  requires 14+. No PASS on 18.6 is claimed here - the first real preflight on
  production is what proves it.

8. OPEN ITEMS
-------------
* audit_ro.rolreplication: documented separately and OPTIONAL. It is unrelated
  to TEMP; no ALTER ROLE is recommended unless the owner asks for it.
* Criterion 3-7 was previously a false positive and is documented as such;
  criterion 3-4 is the real remaining defect until the fix is approved.
* The naive fix4 (plain revoke) is preserved in g5_fix4.zip / FIX_AUDIT_RO_v3.sql
  for audit trail; fix5/v4 is the recommended path.
