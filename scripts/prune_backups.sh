#!/usr/bin/env bash
# Delete old database dumps — explicitly, never automatically.
#
# scripts/backup.sh only creates and verifies a dump. Retention is a separate action
# because a backup run that silently deletes earlier dumps can destroy the very restore
# point it was meant to preserve.
#
# Default behaviour is a dry run: it lists what would go and deletes nothing.
#   bash scripts/prune_backups.sh                    # preview
#   bash scripts/prune_backups.sh --yes              # delete
#   bash scripts/prune_backups.sh --yes --retention-days 14
#
# Requires BACKUP_REMOTE and BACKUP_BUCKET (resolved by
# scripts/resolve_backup_storage.sh). Only files matching newsbot-backup-*.dump under
# <remote>:<bucket>/db/ are considered; nothing else in the bucket is touched.
set -euo pipefail

APPLY=0
RETENTION="${BACKUP_RETENTION_DAYS:-7}"

usage() {
    cat >&2 <<'USAGE'
Usage: bash scripts/prune_backups.sh [--yes] [--retention-days N]

  --yes               actually delete (default: preview only)
  --retention-days N  delete dumps older than N days (default: 7)

Requires BACKUP_REMOTE and BACKUP_BUCKET.
USAGE
}

while [ $# -gt 0 ]; do
    case "$1" in
        --yes)
            APPLY=1
            ;;
        --retention-days)
            shift
            RETENTION="${1:-}"
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "ERROR: unknown argument: $1" >&2
            usage
            exit 2
            ;;
    esac
    shift
done

: "${BACKUP_REMOTE:?BACKUP_REMOTE is required (run scripts/resolve_backup_storage.sh first)}"
: "${BACKUP_BUCKET:?BACKUP_BUCKET is required (run scripts/resolve_backup_storage.sh first)}"

case "$BACKUP_REMOTE" in
    *[[:space:]]*|*/*)
        echo "ERROR: invalid BACKUP_REMOTE '${BACKUP_REMOTE}'; refusing to touch the remote." >&2
        exit 1
        ;;
esac
case "$BACKUP_BUCKET" in
    ""|*[[:space:]]*|*..*|*/*)
        echo "ERROR: invalid BACKUP_BUCKET '${BACKUP_BUCKET}'; refusing to touch the bucket." >&2
        exit 1
        ;;
esac
case "$RETENTION" in
    ""|*[!0-9]*)
        echo "ERROR: --retention-days must be a positive integer, got '${RETENTION}'." >&2
        exit 1
        ;;
esac
if [ "$RETENTION" -lt 1 ]; then
    echo "ERROR: --retention-days must be at least 1, got '${RETENTION}'." >&2
    exit 1
fi

TARGET="${BACKUP_REMOTE}:${BACKUP_BUCKET}/db/"
PATTERN="newsbot-backup-*.dump"

echo "--- Backups under ${TARGET} older than ${RETENTION} day(s) matching ${PATTERN} ---"
if ! old="$(rclone lsf "$TARGET" --files-only --include "$PATTERN" --min-age "${RETENTION}d")"; then
    echo "ERROR: could not list ${TARGET}; nothing was deleted." >&2
    exit 1
fi

if [ -z "$old" ]; then
    echo "Nothing to prune."
    exit 0
fi
printf '%s\n' "$old"

if [ "$APPLY" -ne 1 ]; then
    echo "Dry run: nothing was deleted. Re-run with --yes to delete these files."
    exit 0
fi

echo "--- Deleting ${RETENTION}+ day old dumps from ${TARGET} ---"
if ! rclone delete "$TARGET" --include "$PATTERN" --min-age "${RETENTION}d"; then
    echo "ERROR: rclone delete failed; the listed backups may still exist." >&2
    exit 1
fi
echo "=== Prune completed ==="
