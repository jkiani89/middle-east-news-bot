#!/usr/bin/env bash
# Single source of truth for choosing the backup remote and bucket.
#
# Both .github/workflows/backup.yml and scripts/migrate_with_backup.sh use this, so the
# scheduled backup and the pre-migration backup can never drift to different destinations.
# Behaviour is identical to the block that used to live inline in backup.yml:
#   - BACKUP_STORAGE wins when set; otherwise B2 is inferred from its credentials;
#   - a Backblaze key is scoped to the bucket it can see, so rclone is asked first.
# Writes to $GITHUB_ENV when available; always prints the resolved values.
set -euo pipefail

storage="${BACKUP_STORAGE:-}"
if [ -z "$storage" ]; then
    if [ -n "${RCLONE_CONFIG_B2_ACCOUNT:-}" ]; then
        storage=b2
    else
        storage=r2
    fi
fi

if [ "$storage" = "b2" ]; then
    # A Backblaze application key is scoped to specific buckets and rclone refuses any
    # other name, so the bucket the key can actually reach is authoritative; the secret
    # only covers a key that lists no bucket.
    discovered=$(rclone lsd b2: 2>/dev/null | awk '{print $NF}' | head -n 1 || true)
    bucket="${discovered:-${B2_BUCKET_NAME:-newsbot-backups}}"
else
    bucket="${R2_BUCKET_NAME:-newsbot-backups}"
fi

echo "BACKUP_REMOTE=$storage"
echo "BACKUP_BUCKET=$bucket"
if [ -n "${GITHUB_ENV:-}" ]; then
    {
        echo "BACKUP_REMOTE=$storage"
        echo "BACKUP_BUCKET=$bucket"
    } >> "$GITHUB_ENV"
fi
echo "Selected storage: $storage (bucket: $bucket)"
