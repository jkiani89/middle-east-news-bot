#!/usr/bin/env bash
# Restore drill: prove one stored dump can be restored, without ever touching production.
#
# It downloads exactly one named object from the backup bucket into a temporary directory,
# validates the archive, restores it into a throwaway database on the local runner and
# counts the rows of the real tables. It connects only to a local PostgreSQL, never reads
# DATABASE_URL, never writes to the bucket and drops the throwaway database on any exit.
#
# Usage: bash scripts/restore_drill.sh newsbot-backup-2026-11-17-202309.dump
#
# Environment (all provided by .github/workflows/restore-drill.yml):
#   BACKUP_REMOTE=b2, BACKUP_BUCKET=<bucket>, RCLONE_CONFIG_B2_*  credentials for rclone
#   DRILL_PGHOST/PORT/DATABASE/USER/PASSWORD                       throwaway database
#   DRILL_WAIT_SECONDS                                              optional readiness wait
#
# PostgreSQL 18 is the server that produced the dumps (scripts/backup.sh uses
# postgres:18-alpine), so every client command runs in that same image.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_ROOT"

FILENAME_REGEX='^newsbot-backup-[0-9]{4}-[0-9]{2}-[0-9]{2}-[0-9]{6}\.dump$'
PG_IMAGE='postgres:18-alpine'
ALLOWED_DB='newsbot_restore_check'

# Tables created by alembic/versions/*.py (initial schema, gemini rotator, bulletin slots,
# telegraph reports, article attempt tracking). Missing one means the dump is not this
# project's schema.
EXPECTED_TABLES=(
    sources
    articles
    events
    article_events
    summaries
    daily_reports
    bulletin_slots
    telegraph_reports
    job_logs
    gemini_suspended_keys
    gemini_key_stats
    gemini_request_log
)

log() { printf '%s\n' "$*"; }
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

# ---------------------------------------------------------------------------
# 1. Input validation
# ---------------------------------------------------------------------------
[ "$#" -eq 1 ] || die "usage: bash scripts/restore_drill.sh <backup_filename>"
FILENAME="$1"

if ! [[ "$FILENAME" =~ $FILENAME_REGEX ]]; then
    die "invalid backup filename '${FILENAME}'; expected newsbot-backup-YYYY-MM-DD-HHMMSS.dump"
fi

# ---------------------------------------------------------------------------
# 2. Safety checks: no production connection can be reached from here
# ---------------------------------------------------------------------------
if [ -n "${DATABASE_URL:-}" ]; then
    die "DATABASE_URL is set; the drill refuses to run with a production connection string in the environment"
fi

TARGET_HOST="${DRILL_PGHOST:-127.0.0.1}"
TARGET_PORT="${DRILL_PGPORT:-5432}"
TARGET_DB="${DRILL_PGDATABASE:-$ALLOWED_DB}"
TARGET_USER="${DRILL_PGUSER:-drill}"
TARGET_PASSWORD="${DRILL_PGPASSWORD:-drill_password}"

for host in "${PGHOST:-}" "${TARGET_HOST}"; do
    case "$host" in
        ''|127.0.0.1|localhost) ;;
        *) die "refusing non-local database host '${host}'; only 127.0.0.1 or localhost is allowed" ;;
    esac
done

[ "$TARGET_DB" = "$ALLOWED_DB" ] || die "refusing database '${TARGET_DB}'; only ${ALLOWED_DB} is allowed"
[ -n "$TARGET_USER" ] || die "DRILL_PGUSER must not be empty"

REMOTE="${BACKUP_REMOTE:-b2}"
[ "$REMOTE" = 'b2' ] || die "refusing remote '${REMOTE}'; this drill only reads from b2"

BUCKET="${BACKUP_BUCKET:-${B2_BUCKET_NAME:-}}"
[ -n "$BUCKET" ] || die "BACKUP_BUCKET (or B2_BUCKET_NAME) is required"
[[ "$BUCKET" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]] || die "invalid bucket name '${BUCKET}'"

command -v rclone >/dev/null 2>&1 || die "rclone not found; it is needed to download the object"
command -v docker >/dev/null 2>&1 || die "docker not found; it runs pg_restore/psql (${PG_IMAGE})"

OBJECT="${REMOTE}:${BUCKET}/db/${FILENAME}"

# ---------------------------------------------------------------------------
# 3. Temporary workspace and guaranteed cleanup
# ---------------------------------------------------------------------------
WORKDIR="$(mktemp -d "${TMPDIR:-/tmp}/restore-drill.XXXXXX")"
DUMP="${WORKDIR}/${FILENAME}"

docker_pg() {
    docker run --rm -i --network host -e "PGPASSWORD=${TARGET_PASSWORD}" "$PG_IMAGE" "$@"
}

psql_query() {
    docker_pg psql -h "$TARGET_HOST" -p "$TARGET_PORT" -U "$TARGET_USER" \
        -d "$1" -v ON_ERROR_STOP=1 -tA -F $'\t' -c "$2"
}

drop_drill_database() {
    # Best effort: the runner may already be shutting down, and this must never touch B2.
    psql_query postgres "DROP DATABASE IF EXISTS \"${TARGET_DB}\" WITH (FORCE);" >/dev/null 2>&1 || true
}

cleanup() {
    status=$?
    trap - EXIT
    drop_drill_database
    rm -rf "$WORKDIR"
    log "--- Cleanup: drill database dropped, downloaded dump removed from the runner ---"
    exit "$status"
}
trap cleanup EXIT INT TERM

# ---------------------------------------------------------------------------
# 4. Readiness of the throwaway server
# ---------------------------------------------------------------------------
wait_seconds="${DRILL_WAIT_SECONDS:-60}"
deadline=$((SECONDS + wait_seconds))
until psql_query postgres 'SELECT 1;' >/dev/null 2>&1; do
    [ "$SECONDS" -lt "$deadline" ] || die "the local drill database at ${TARGET_HOST}:${TARGET_PORT} never became ready"
    sleep 2
done

# ---------------------------------------------------------------------------
# 5. Download exactly the requested object (no listing, no sync, no delete)
# ---------------------------------------------------------------------------
log "--- Downloading ${OBJECT} ---"
if ! rclone copyto "$OBJECT" "$DUMP"; then
    die "could not download '${FILENAME}'; the application key may lack readFiles for this bucket"
fi

size="$(wc -c < "$DUMP" | tr -d ' ')"
[ "${size:-0}" -gt 0 ] || die "downloaded dump is empty; refusing to treat it as a backup"
log "--- Downloaded ${size} bytes ---"

# ---------------------------------------------------------------------------
# 6. Structural validation of the PostgreSQL custom-format archive
# ---------------------------------------------------------------------------
log '--- Validating archive with pg_restore --list ---'
toc="$(docker_pg pg_restore --list < "$DUMP")" || die "pg_restore --list rejected '${FILENAME}'; the archive is unreadable"
toc_entries="$(printf '%s\n' "$toc" | grep -cE '^[0-9]+;' || true)"
[ "${toc_entries:-0}" -gt 0 ] || die 'pg_restore --list returned no table-of-contents entries'
log "--- Archive contains ${toc_entries} TOC entries ---"

# ---------------------------------------------------------------------------
# 7. Restore into a fresh, empty throwaway database
# ---------------------------------------------------------------------------
log '--- Recreating the throwaway database ---'
psql_query postgres "DROP DATABASE IF EXISTS \"${TARGET_DB}\" WITH (FORCE);" >/dev/null
psql_query postgres "CREATE DATABASE \"${TARGET_DB}\";" >/dev/null

log '--- Restoring (no-owner, no-privileges, exit-on-error) ---'
docker_pg pg_restore -h "$TARGET_HOST" -p "$TARGET_PORT" -U "$TARGET_USER" \
    -d "$TARGET_DB" --no-owner --no-privileges --exit-on-error < "$DUMP" \
    || die "pg_restore failed; the archived dump cannot be restored"

# ---------------------------------------------------------------------------
# 8. Content validation: schema, alembic revision, row counts, constraints
# ---------------------------------------------------------------------------
log '--- Checking the restored schema ---'
actual_tables="$(psql_query "$TARGET_DB" "SELECT table_name FROM information_schema.tables WHERE table_schema='public' ORDER BY table_name;")"
missing=()
for table in "${EXPECTED_TABLES[@]}"; do
    if ! printf '%s\n' "$actual_tables" | grep -Fxq "$table"; then
        missing+=("$table")
    fi
done
if [ "${#missing[@]}" -gt 0 ]; then
    die "restored database is missing table(s): ${missing[*]}"
fi
log "--- All ${#EXPECTED_TABLES[@]} expected tables are present ---"

version="$(psql_query "$TARGET_DB" 'SELECT version_num FROM alembic_version;')"
[ -n "$version" ] || die 'alembic_version is empty; the dump does not record a schema revision'
log "--- alembic_version: ${version} ---"

log '--- Row counts (counts only, never row contents) ---'
counts="$(psql_query "$TARGET_DB" "SELECT 'sources', count(*) FROM sources UNION ALL SELECT 'articles', count(*) FROM articles UNION ALL SELECT 'events', count(*) FROM events UNION ALL SELECT 'article_events', count(*) FROM article_events UNION ALL SELECT 'summaries', count(*) FROM summaries UNION ALL SELECT 'daily_reports', count(*) FROM daily_reports UNION ALL SELECT 'bulletin_slots', count(*) FROM bulletin_slots UNION ALL SELECT 'telegraph_reports', count(*) FROM telegraph_reports UNION ALL SELECT 'job_logs', count(*) FROM job_logs UNION ALL SELECT 'gemini_suspended_keys', count(*) FROM gemini_suspended_keys UNION ALL SELECT 'gemini_key_stats', count(*) FROM gemini_key_stats UNION ALL SELECT 'gemini_request_log', count(*) FROM gemini_request_log;")"
printf '%s\n' "$counts" | while IFS=$'\t' read -r table count; do
    [ -n "${table:-}" ] && printf '  %-22s %s\n' "$table" "${count:-0}"
done

log '--- Constraints ---'
constraints="$(psql_query "$TARGET_DB" "SELECT contype, count(*) FROM pg_constraint WHERE connamespace='public'::regnamespace GROUP BY contype ORDER BY contype;")"
foreign_keys="$(printf '%s\n' "$constraints" | awk '$1=="f" {print $2; found=1} END {if (!found) print 0}')"
primary_keys="$(printf '%s\n' "$constraints" | awk '$1=="p" {print $2; found=1} END {if (!found) print 0}')"
[ "${foreign_keys:-0}" -gt 0 ] || die 'the restored database has no foreign keys; the schema is incomplete'
log "  primary keys: ${primary_keys:-0}"
log "  foreign keys: ${foreign_keys}"

log "=== Restore drill PASSED for ${FILENAME} (${size} bytes, revision ${version}) ==="
