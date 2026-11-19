#!/usr/bin/env bash
# Local, throwaway validation of the G5 criteria logic. No connection to Aiven, no
# project data. Dev-only helper, not part of the audited package.
#
# Requires: /tmp/g5v venv with asyncpg==0.32.0 + pgserver.
# NOTE: the bundled PostgreSQL 16.2 build has no OpenSSL, so this harness exercises
# QUERIES + evaluate() + overall() but not the TLS gate of main(). The TLS gate is
# unchanged by fix3 and is exercised on the real audited cluster.
set -u
BIN=/tmp/g5v/lib/python3.12/site-packages/pgserver/pginstall/bin
DATA=/tmp/pgdata_g5f3
PORT=55432
IP=169.254.77.2
HANDOFF=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
PY=/tmp/g5v/bin/python

rm -rf "$DATA"
echo "### initdb"
"$BIN/initdb" -D "$DATA" -U postgres --auth=trust -E UTF8 >/tmp/g5_initdb.log 2>&1 || { tail -20 /tmp/g5_initdb.log; exit 1; }

cat >> "$DATA/postgresql.conf" <<CONF
listen_addresses = '*'
port = $PORT
CONF
echo "host all all 0.0.0.0/0 trust" >> "$DATA/pg_hba.conf"

echo "### start"
"$BIN/pg_ctl" -D "$DATA" -l /tmp/g5_pg.log -w start || { tail -30 /tmp/g5_pg.log; exit 1; }

PSQL="$BIN/psql -h /tmp -p $PORT -U postgres -d postgres -v ON_ERROR_STOP=1 -X -q"

echo "### fixtures (throwaway cluster only)"
$PSQL -c "CREATE ROLE audit_ro LOGIN PASSWORD 'local_test_only' NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;" || exit 1
$PSQL -c "CREATE TABLE public.articles (id serial primary key, title text not null);" || exit 1
$PSQL -c "CREATE TABLE public.sources  (id serial primary key, name  text not null);" || exit 1
$PSQL -c "INSERT INTO public.articles (title) VALUES ('baseline-row-must-survive');" || exit 1

echo "### baseline state (as postgres)"
$PSQL -c "SELECT count(*) AS articles_rows FROM public.articles;"
$PSQL -c "SELECT has_database_privilege('audit_ro', current_database(), 'TEMPORARY') AS audit_ro_temp, has_database_privilege('public', current_database(), 'TEMPORARY') AS public_temp, datacl FROM pg_database WHERE datname = current_database();"
$PSQL -c "SELECT has_table_privilege('audit_ro','public.articles','SELECT') AS sel, has_table_privilege('audit_ro','public.articles','INSERT') AS ins, has_table_privilege('audit_ro','public.articles','UPDATE') AS upd;"

DSN="postgresql://audit_ro:local_test_only@$IP:$PORT/postgres"

echo; echo "########## RUN A: fix2 criteria (baseline) ##########"
$PY "$HANDOFF/local_validation/run_query_harness.py" "$HANDOFF/g5_probe_fix2.py" "$DSN" fix2

echo; echo "########## RUN B: fix3 criteria (3-7 narrowed) ##########"
$PY "$HANDOFF/local_validation/run_query_harness.py" "$HANDOFF/g5_probe_fix3.py" "$DSN" fix3

echo; echo "########## APPLY THE PROPOSED 3-4 REMEDY ON THIS THROWAWAY CLUSTER ONLY ##########"
$PSQL -c "REVOKE TEMPORARY ON DATABASE postgres FROM PUBLIC;" || exit 1
$PSQL -c "SELECT has_database_privilege('audit_ro', current_database(), 'TEMPORARY') AS audit_ro_temp_after, has_database_privilege('postgres', current_database(), 'TEMPORARY') AS owner_temp_after, datacl FROM pg_database WHERE datname = current_database();"

echo; echo "########## RUN C: fix3 criteria after the remedy ##########"
$PY "$HANDOFF/local_validation/run_query_harness.py" "$HANDOFF/g5_probe_fix3.py" "$DSN" fix3_after

echo "########## read-only proof: data unchanged ##########"
$PSQL -c "SELECT id, title FROM public.articles ORDER BY id;"
$PSQL -c "SELECT count(*) AS articles_rows_after FROM public.articles;"

echo "### stop"
"$BIN/pg_ctl" -D "$DATA" -m fast -w stop
echo "DONE"
