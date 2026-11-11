#!/usr/bin/env bash
# Backup PostgreSQL database to Cloudflare R2 via rclone.
# Requires: docker compose (running), rclone configured with remote named "r2".
# Usage: bash scripts/backup.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_ROOT"

# Check if .env exists
if [ ! -f .env ]; then
    echo "ERROR: .env not found in $PROJECT_ROOT" >&2
    exit 1
fi

# Load environment variables safely
set -a
# shellcheck disable=SC1091
source .env
set +a

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
echo "--- Dumping database ${DB_NAME} ---"

# Using -T for docker compose exec to avoid TTY issues in scripts.
# Redirecting stdout to the local file to avoid permission issues inside container.
docker compose exec -T postgres pg_dump -U "$DB_USER" -Fc "$DB_NAME" > "$TMP_PATH"

echo "--- Uploading to R2: r2:${R2_BUCKET}/db/${FILENAME} ---"
rclone copy "$TMP_PATH" "r2:${R2_BUCKET}/db/"

# 2. Cleanup local temp file
rm -f "$TMP_PATH"
echo "--- Local temp file removed ---"

# 3. Cleanup old backups in R2
echo "--- Removing R2 backups older than ${RETENTION} days ---"
rclone delete "r2:${R2_BUCKET}/db/" --min-age "${RETENTION}d"

echo "=== Backup completed successfully ==="
