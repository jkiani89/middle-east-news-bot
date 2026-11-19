#!/usr/bin/env bash
# Local, throwaway validation of the SQL package (PREFLIGHT + FIX) and of the
# fix3 probe against a cluster that mimics production names (defaultdb,
# audit_ro, avnadmin, _avnadmin_monitor). Dev-only helper, not in the ZIP.
#
# The GRANT/REVOKE below run against THIS throwaway cluster only. Nothing here
# touches Aiven or project data.
set -u
BIN=/tmp/g5v/lib/python3.12/site-packages/pgserver/pginstall/bin
DATA=/tmp/pgdata_g5sql
PORT=55433
IP=169.254.77.2
HANDOFF=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
PY=/tmp/g5v/bin/python

rm -rf "$DATA"
echo "### initdb"
"$BIN/initdb" -D "$DATA" -U postgres --auth=trust -E UTF8 >/tmp/g5sql_initdb.log 2>&1 || { tail -20 /tmp/g5sql_initdb.log; exit 1; }
cat >> "$DATA/postgresql.conf" <<CONF
listen_addresses = '*'
port = $PORT
CONF
echo "host all all 0.0.0.0/0 trust" >> "$DATA/pg_hba.conf"
"$BIN/pg_ctl" -D "$DATA" -l /tmp/g5sql_pg.log -w start || { tail -30 /tmp/g5sql_pg.log; exit 1; }

PSQL="$BIN/psql -h /tmp -p $PORT -U postgres -v ON_ERROR_STOP=1 -X -q"

echo "### mimic production topology (throwaway)"
$PSQL -c "CREATE ROLE avnadmin LOGIN CREATEDB CREATEROLE;" || exit 1
$PSQL -c "CREATE ROLE _avnadmin_monitor LOGIN;" || exit 1
$PSQL -c "CREATE ROLE audit_ro LOGIN PASSWORD 'local_test_only' NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;" || exit 1
$PSQL -c "CREATE DATABASE defaultdb OWNER avnadmin;" || exit 1
# Reproduce Aiven's explicit grants, which materialise datacl while keeping PUBLIC's default entry.
$PSQL -d defaultdb -c "GRANT ALL ON DATABASE defaultdb TO avnadmin;" || exit 1
$PSQL -d defaultdb -c "GRANT CONNECT, TEMPORARY ON DATABASE defaultdb TO _avnadmin_monitor;" || exit 1
$PSQL -d defaultdb -c "CREATE TABLE public.articles (id serial primary key, title text not null);" || exit 1
$PSQL -d defaultdb -c "INSERT INTO public.articles (title) VALUES ('baseline-row-must-survive');" || exit 1

echo; echo "########## 1) PREFLIGHT (read-only) ##########"
$PSQL -d defaultdb -f "$HANDOFF/PREFLIGHT_AUDIT_RO_v2.sql" || exit 1

echo; echo "########## 2) probe fix3 BEFORE the remedy ##########"
DSN="postgresql://audit_ro:local_test_only@$IP:$PORT/defaultdb"
$PY "$HANDOFF/local_validation/run_query_harness.py" "$HANDOFF/g5_probe_fix3.py" "$DSN" sql_before || exit 1

echo; echo "########## 3) FIX v2 (throwaway cluster only) ##########"
$PSQL -d defaultdb -f "$HANDOFF/FIX_AUDIT_RO_v2.sql" || exit 1

echo; echo "########## 4) PREFLIGHT again (impact must be reclassified) ##########"
$PSQL -d defaultdb -f "$HANDOFF/PREFLIGHT_AUDIT_RO_v2.sql" || exit 1

echo; echo "########## 5) probe fix3 AFTER the remedy ##########"
$PY "$HANDOFF/local_validation/run_query_harness.py" "$HANDOFF/g5_probe_fix3.py" "$DSN" sql_after || exit 1

echo; echo "########## 6) data untouched ##########"
$PSQL -d defaultdb -c "SELECT id, title FROM public.articles ORDER BY id;"

echo "### stop"
"$BIN/pg_ctl" -D "$DATA" -m fast -w stop
echo "DONE"
