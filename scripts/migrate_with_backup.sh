#!/usr/bin/env bash
# Fail-closed migration gate.
#
# Pending Alembic migrations are applied only after the dump produced by *this* run was
# validated with pg_restore --list, uploaded, and verified at the destination. The gate
# binds to the exact artifact scripts/backup.sh reports (name, today's date, byte size),
# so a leftover dump from an earlier backup can never satisfy it.
#
# When nothing is pending the gate exits 0 without touching the database or the bucket,
# so ordinary six-hourly runs create no backup and do not fill the free tier.
#
# Environment:
#   BACKUP_SCRIPT          override for tests (default: scripts/backup.sh)
#   BACKUP_REMOTE          remote resolved by scripts/resolve_backup_storage.sh (e.g. b2/r2)
#   BACKUP_BUCKET          bucket resolved by the same script
#   ALLOW_UNINITIALIZED_DB "1" permits migrating a database with no alembic_version row
set -euo pipefail

BACKUP_SCRIPT="${BACKUP_SCRIPT:-scripts/backup.sh}"
ALLOW_UNINITIALIZED_DB="${ALLOW_UNINITIALIZED_DB:-0}"

fail() {
    echo "::error::$*" >&2
    exit 1
}

valid_revision() {
    # Alembic revision ids are a single alphanumeric/underscore token. Anything else
    # (a warning line, a path, an error echoed to stdout) is not a revision we can trust.
    case "$1" in
        "") return 1 ;;
        *[!0-9A-Za-z_]*) return 1 ;;
        *) return 0 ;;
    esac
}

# 1. Resolve both sides of the migration. Any failure or ambiguity stops here, before a
#    backup is attempted and long before the schema is touched.
if ! current_raw="$(alembic current 2>/dev/null)"; then
    fail "alembic current failed; cannot determine the schema revision."
fi
if ! heads_raw="$(alembic heads 2>/dev/null)"; then
    fail "alembic heads failed; cannot determine the target revision."
fi

current_lines="$(printf '%s\n' "$current_raw" | awk 'NF')"
heads="$(printf '%s\n' "$heads_raw" | awk 'NF{print $1}')"
head_count="$(printf '%s\n' "$heads_raw" | awk 'NF' | wc -l | tr -d ' ')"
current_count="$(printf '%s\n' "$current_raw" | awk 'NF' | wc -l | tr -d ' ')"

if [ "$head_count" -ne 1 ]; then
    fail "Expected exactly one Alembic head, found ${head_count}: ${heads:-none}"
fi
if ! valid_revision "$heads"; then
    fail "Unrecognized Alembic head output: ${heads}"
fi

if [ "$current_count" -gt 1 ]; then
    fail "Database reports ${current_count} current revisions; refusing to guess which one is live."
fi

if [ "$current_count" -eq 0 ]; then
    if [ "$ALLOW_UNINITIALIZED_DB" != "1" ]; then
        fail "Database has no Alembic revision (wrong DATABASE_URL or an uninitialized schema). Set ALLOW_UNINITIALIZED_DB=1 only for a brand-new database."
    fi
    current=""
else
    current="$(printf '%s\n' "$current_lines" | awk 'NR==1{print $1}')"
    if ! valid_revision "$current"; then
        fail "Unrecognized Alembic current output: ${current_lines}"
    fi
fi

if [ -n "$current" ] && [ "$current" = "$heads" ]; then
    echo "Schema is current at ${heads}; no backup needed."
    exit 0
fi

: "${BACKUP_REMOTE:?BACKUP_REMOTE is required to verify the uploaded dump}"
: "${BACKUP_BUCKET:?BACKUP_BUCKET is required to verify the uploaded dump}"

# 2. Back up. A non-zero exit, or a dump the destination does not hold, stops the run.
echo "Pending migration: ${current:-<none>} -> ${heads}; taking a verified backup first."
if ! backup_output="$(bash "$BACKUP_SCRIPT" 2>&1)"; then
    printf '%s\n' "$backup_output"
    fail "Backup failed; refusing to migrate."
fi
printf '%s\n' "$backup_output"

uploaded="$(printf '%s\n' "$backup_output" | sed -n 's/^BACKUP_FILENAME=//p' | tail -n 1)"
local_bytes="$(printf '%s\n' "$backup_output" | sed -n 's/^BACKUP_SIZE=//p' | tail -n 1)"

if [ -z "$uploaded" ]; then
    fail "Backup did not report the artifact it produced; refusing to migrate."
fi
if [ -z "$local_bytes" ]; then
    fail "Backup did not report the dump size; refusing to migrate."
fi

today="$(date -u +%Y-%m-%d)"
case "$uploaded" in
    newsbot-backup-"$today"-*.dump) ;;
    *)
        fail "Backup artifact ${uploaded} does not belong to today (${today}); refusing to migrate."
        ;;
esac

# 3. Verify that exact artifact, not merely "some dump from today".
if ! remote_json="$(rclone size --json "${BACKUP_REMOTE}:${BACKUP_BUCKET}/db/${uploaded}")"; then
    fail "Could not read ${uploaded} at ${BACKUP_REMOTE}:${BACKUP_BUCKET}; refusing to migrate."
fi
remote_bytes="$(printf '%s' "$remote_json" | sed -n 's/.*"bytes":\([0-9]*\).*/\1/p')"
if [ "${remote_bytes:-}" != "$local_bytes" ]; then
    fail "Uploaded ${uploaded} is ${remote_bytes:-missing} bytes at the destination but the local dump was ${local_bytes}; refusing to migrate."
fi
echo "Verified backup: ${uploaded} (${remote_bytes} bytes at ${BACKUP_REMOTE}:${BACKUP_BUCKET})"

alembic upgrade head
echo "Migration applied: ${current:-<none>} -> ${heads}"
