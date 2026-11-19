#!/usr/bin/env bash
# Local, throwaway validation of PREFLIGHT_AUDIT_RO_v3.sql / FIX_AUDIT_RO_v3.sql.
# Dev-only helper (not shipped in the ZIP). Everything below runs on a private
# cluster in /tmp; nothing here touches Aiven or project data.
set -u
BIN=/tmp/g5v/lib/python3.12/site-packages/pgserver/pginstall/bin
DATA=/tmp/pgdata_g5v3
PORT=55441
IP=169.254.77.2
HANDOFF=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
PY=/tmp/g5v/bin/python

rm -rf "$DATA"
echo "### initdb"
"$BIN/initdb" -D "$DATA" -U postgres --auth=trust -E UTF8 >/tmp/g5v3_initdb.log 2>&1 || { tail -20 /tmp/g5v3_initdb.log; exit 1; }
printf 'listen_addresses = %s\nport = %s\n' "'*'" "$PORT" >> "$DATA/postgresql.conf"
echo "host all all 0.0.0.0/0 trust" >> "$DATA/pg_hba.conf"
"$BIN/pg_ctl" -D "$DATA" -l /tmp/g5v3_pg.log -w start || { tail -30 /tmp/g5v3_pg.log; exit 1; }

PSQL="$BIN/psql -h /tmp -p $PORT -v ON_ERROR_STOP=1 -X -q"
ADM="$PSQL -U avnadmin -d defaultdb"

echo "### mimic production topology (throwaway)"
$PSQL -U postgres -c "CREATE ROLE avnadmin LOGIN CREATEDB CREATEROLE;" || exit 1
$PSQL -U postgres -c "CREATE ROLE _avnadmin_monitor LOGIN;" || exit 1
$PSQL -U postgres -c "CREATE ROLE audit_ro LOGIN PASSWORD 'local_test_only' NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;" || exit 1
$PSQL -U postgres -c "CREATE DATABASE defaultdb OWNER avnadmin;" || exit 1
$ADM -c "GRANT ALL ON DATABASE defaultdb TO avnadmin;" || exit 1
$ADM -c "GRANT CONNECT, TEMPORARY ON DATABASE defaultdb TO _avnadmin_monitor;" || exit 1
$ADM -c "CREATE TABLE public.articles (id serial primary key, title text not null);" || exit 1
$ADM -c "INSERT INTO public.articles (title) VALUES ('baseline-row-must-survive');" || exit 1

echo; echo "########## 1) PREFLIGHT v3 as avnadmin (read-only) ##########"
$ADM -f "$HANDOFF/PREFLIGHT_AUDIT_RO_v3.sql" || exit 1

echo; echo "########## 2) READ-ONLY PROOF: a write inside the transaction must fail with 25006 ##########"
cat > /tmp/g5v3_rodtest.sql <<'SQL'
BEGIN TRANSACTION READ ONLY;
SELECT current_setting('transaction_read_only') AS guard;
CREATE TEMP TABLE g5_should_never_exist(i int);
ROLLBACK;
SQL
set +e
ROOUT=$($ADM -f /tmp/g5v3_rodtest.sql 2>&1)
ROEXIT=$?
set -e
echo "$ROOUT"
echo "read_only_write_attempt_exit=$ROEXIT (non-zero = the server refused the write)"
echo "$ROOUT" | grep -qi "read-only transaction" && echo "PROOF: server rejected the write (read-only transaction)" || echo "PROOF MISSING: write was not rejected"

echo; echo "########## 3) probe fix3 BEFORE the remedy ##########"
DSN="postgresql://audit_ro:local_test_only@$IP:$PORT/defaultdb"
$PY "$HANDOFF/local_validation/run_query_harness.py" "$HANDOFF/g5_probe_fix3.py" "$DSN" sql_before_v3 || exit 1

echo; echo "########## 4) ROLLBACK PROOF: make the monitor role depend on PUBLIC, so the post-REVOKE assert must fail ##########"
$ADM -c "REVOKE TEMPORARY ON DATABASE defaultdb FROM _avnadmin_monitor;" || exit 1
set +e
FAILOUT=$($ADM -f "$HANDOFF/FIX_AUDIT_RO_v3.sql" 2>&1)
FAILEXIT=$?
set -e
echo "$FAILOUT" | tail -12
echo "fix_with_broken_assert_exit=$FAILEXIT (non-zero expected)"
$PSQL -U postgres -d defaultdb -c "SELECT datacl::text AS datacl_after_failed_fix,
       has_database_privilege('public', current_database(), 'TEMPORARY') AS public_temp_still_true
FROM pg_database WHERE datname = current_database();"

echo; echo "########## 5) restore the monitor grant, run the real FIX v3 ##########"
$ADM -c "GRANT TEMPORARY ON DATABASE defaultdb TO _avnadmin_monitor;" || exit 1
$ADM -f "$HANDOFF/FIX_AUDIT_RO_v3.sql" || exit 1

echo; echo "########## 6) PREFLIGHT v3 again (no role may be classified LOSES) ##########"
$ADM -f "$HANDOFF/PREFLIGHT_AUDIT_RO_v3.sql" || exit 1

echo; echo "########## 7) probe fix3 AFTER the remedy ##########"
$PY "$HANDOFF/local_validation/run_query_harness.py" "$HANDOFF/g5_probe_fix3.py" "$DSN" sql_after_v3 || exit 1

echo; echo "########## 8) data untouched ##########"
$PSQL -U postgres -d defaultdb -c "SELECT id, title FROM public.articles ORDER BY id;"

echo "### stop"
"$BIN/pg_ctl" -D "$DATA" -m fast -w stop
echo "DONE"
