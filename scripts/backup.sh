#!/usr/bin/env bash
# Backup PostgreSQL database to object storage via rclone (Cloudflare R2 or Backblaze B2).
# Requires: docker, rclone configured with the remote named by BACKUP_REMOTE.
# Usage: bash scripts/backup.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_ROOT"

# Load environment variables from .env if present
if [ -f .env ]; then
    set -a
    # shellcheck disable=SC1091
    source .env
    set +a
fi

if [ -z "${DATABASE_URL:-}" ] && [ -z "${POSTGRES_DB:-}" ]; then
    echo "ERROR: Neither .env file nor DATABASE_URL / POSTGRES_DB environment variable is set." >&2
    exit 1
fi

# Defaults
DB_USER="${POSTGRES_USER:-newsbot}"
DB_NAME="${POSTGRES_DB:-newsbot}"

# GitHub Actions resolves the destination with scripts/resolve_backup_storage.sh and exports
# BACKUP_REMOTE/BACKUP_BUCKET, so the fallbacks below stay for that path only. A local run
# must name its destination explicitly: silently defaulting to the production bucket would
# upload a development dump next to real backups.
if [ "${GITHUB_ACTIONS:-}" = "true" ]; then
    BACKUP_REMOTE="${BACKUP_REMOTE:-r2}"
    BACKUP_BUCKET="${BACKUP_BUCKET:-${R2_BUCKET_NAME:-${B2_BUCKET_NAME:-newsbot-backups}}}"
else
    if [ -z "${BACKUP_REMOTE:-}" ] || [ -z "${BACKUP_BUCKET:-}" ]; then
        echo "ERROR: BACKUP_REMOTE and BACKUP_BUCKET must both be set for a local backup run; refusing to guess a destination." >&2
        echo "Set them explicitly, for example:" >&2
        echo "  BACKUP_REMOTE=b2 BACKUP_BUCKET=your-bucket bash scripts/backup.sh" >&2
        echo "In GitHub Actions this is resolved by scripts/resolve_backup_storage.sh." >&2
        exit 1
    fi
fi

# This script never deletes anything: retention is a separate, explicit action
# (scripts/prune_backups.sh, which requires --yes). A backup that silently removes
# earlier dumps cannot be used as a restore point.

# Check if rclone is installed
if ! command -v rclone >/dev/null 2>&1; then
    echo "ERROR: rclone not found. Install it first." >&2
    exit 1
fi

# 1. Create backup
TIMESTAMP=$(date -u +"%Y-%m-%d-%H%M%S")
FILENAME="newsbot-backup-${TIMESTAMP}.dump"
TMP_PATH="/tmp/${FILENAME}"

echo "=== Starting backup: ${FILENAME} ==="

if [ -n "${DATABASE_URL:-}" ]; then
    # pg_dump refuses to dump a newer server, so the client image must match the
    # server's major version (the managed database runs PostgreSQL 18).
    echo "--- Dumping database from DATABASE_URL via postgres:18-alpine container ---"
    SYNC_DB_URL="${DATABASE_URL//+asyncpg/}"
    docker run --rm -i postgres:18-alpine pg_dump -d "$SYNC_DB_URL" -Fc > "$TMP_PATH"
else
    echo "--- Dumping database ${DB_NAME} via local postgres container ---"
    docker compose exec -T postgres pg_dump -U "$DB_USER" -Fc "$DB_NAME" > "$TMP_PATH"
fi

if [ ! -s "$TMP_PATH" ]; then
    echo "ERROR: pg_dump produced an empty file; refusing to upload it." >&2
    rm -f "$TMP_PATH"
    exit 1
fi
local_size="$(wc -c < "$TMP_PATH" | tr -d ' ')"
echo "--- Dump size: ${local_size} bytes ---"
# Machine-readable identity of this run's artifact, consumed by
# scripts/migrate_with_backup.sh so the gate verifies this dump and not an older one.
echo "BACKUP_FILENAME=${FILENAME}"
echo "BACKUP_SIZE=${local_size}"

# The dump must be readable by pg_restore before it counts as a backup: a truncated or
# half-written file uploads fine and only fails on the day it is needed.
echo "--- Validating dump with pg_restore --list ---"
if ! docker run --rm -i postgres:18-alpine pg_restore --list < "$TMP_PATH" > /dev/null; then
    echo "ERROR: pg_restore --list rejected ${FILENAME}; refusing to upload an unreadable dump." >&2
    rm -f "$TMP_PATH"
    exit 1
fi

echo "--- Uploading to ${BACKUP_REMOTE}:${BACKUP_BUCKET}/db/${FILENAME} ---"
rclone copy "$TMP_PATH" "${BACKUP_REMOTE}:${BACKUP_BUCKET}/db/"

# 2. Verify the object landed at the destination with the same size, then clean up.
#    rclone copy already performs its post-copy checksum check by default (B2 stores
#    SHA1), so this guards the object's identity/presence; it does not re-download it.
remote_size="$(rclone size --json "${BACKUP_REMOTE}:${BACKUP_BUCKET}/db/${FILENAME}" | sed -n 's/.*"bytes":\([0-9]*\).*/\1/p')"
if [ "${remote_size:-}" != "$local_size" ]; then
    echo "ERROR: uploaded object size (${remote_size:-unknown}) does not match the local dump (${local_size}) at ${BACKUP_REMOTE}:${BACKUP_BUCKET}/db/${FILENAME}." >&2
    rm -f "$TMP_PATH"
    exit 1
fi
echo "--- Verified remote object: ${BACKUP_REMOTE}:${BACKUP_BUCKET}/db/${FILENAME} (${local_size} bytes) ---"

rm -f "$TMP_PATH"
echo "--- Local temp file removed ---"

echo "=== Backup completed successfully ==="
