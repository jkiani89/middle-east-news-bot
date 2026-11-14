#!/usr/bin/env bash
# Backup PostgreSQL database to Cloudflare R2 via rclone.
# Requires: docker compose (running), rclone configured with remote named "r2".
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
R2_BUCKET="${R2_BUCKET_NAME:-newsbot-backups}"
RETENTION="${BACKUP_RETENTION_DAYS:-7}"

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

echo "--- Uploading to R2: r2:${R2_BUCKET}/db/${FILENAME} ---"
rclone copy "$TMP_PATH" "r2:${R2_BUCKET}/db/"

# 2. Cleanup local temp file
rm -f "$TMP_PATH"
echo "--- Local temp file removed ---"

# 3. Cleanup old backups in R2
echo "--- Removing R2 backups older than ${RETENTION} days ---"
rclone delete "r2:${R2_BUCKET}/db/" --min-age "${RETENTION}d"

echo "=== Backup completed successfully ==="
