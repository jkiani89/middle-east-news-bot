#!/usr/bin/env bash
# Restore PostgreSQL database from a backup file (local or Cloudflare R2).
# WARNING: This will DROP and recreate all tables (--clean --if-exists).
# Usage: bash scripts/restore.sh <backup_filename>

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_ROOT"

if [ "$#" -ne 1 ]; then
    echo "Usage: $0 <backup_filename>" >&2
    echo "Example: $0 newsbot-backup-2026-11-11-120000.dump" >&2
    exit 1
fi

FILENAME="$1"
TMP_PATH="/tmp/${FILENAME}"

# Load environment variables from .env if present
if [ -f .env ]; then
    set -a
    # shellcheck disable=SC1091
    source .env
    set +a
fi

DB_USER="${POSTGRES_USER:-newsbot}"
DB_NAME="${POSTGRES_DB:-newsbot}"
R2_BUCKET="${R2_BUCKET_NAME:-newsbot-backups}"

# 1. Download if not present locally
if [ ! -f "$TMP_PATH" ]; then
    echo "--- Downloading ${FILENAME} from R2 ---"
    if ! command -v rclone >/dev/null 2>&1; then
        echo "ERROR: rclone not found. Install it or place the backup file in /tmp/" >&2
        exit 1
    fi
    rclone copy "r2:${R2_BUCKET}/db/${FILENAME}" "/tmp/"
fi

# 2. Confirmation prompt
echo "====================================================="
echo "WARNING: You are about to RESTORE database '${DB_NAME}'."
echo "This will DROP and recreate all existing tables."
echo "====================================================="
read -p "Are you absolutely sure? Type 'yes' to continue: " CONFIRM

if [ "$CONFIRM" != "yes" ]; then
    echo "Restore aborted by user."
    rm -f "$TMP_PATH"
    exit 1
fi

# 3. Restore
echo "--- Restoring database ${DB_NAME} ---"
if [ -n "${DATABASE_URL:-}" ]; then
    SYNC_DB_URL="${DATABASE_URL//+asyncpg/}"
    docker run --rm -i postgres:16-alpine pg_restore -d "$SYNC_DB_URL" --clean --if-exists < "$TMP_PATH"
else
    docker compose exec -T postgres pg_restore -U "$DB_USER" -d "$DB_NAME" --clean --if-exists < "$TMP_PATH"
fi

# 4. Cleanup
rm -f "$TMP_PATH"
echo "=== Restore completed successfully ==="
