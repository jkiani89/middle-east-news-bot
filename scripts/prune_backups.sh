#!/usr/bin/env bash
# Delete old database dumps — explicitly, never automatically.
#
# scripts/backup.sh only creates and verifies a dump. Retention is a separate action
# because a backup run that silently deletes earlier dumps can destroy the very restore
# point it was meant to preserve.
#
# Safety rules:
#   * the newest BACKUP_MIN_KEEP (default 3) dumps are always kept, however old they are;
#   * a dump is only eligible when it is strictly older than BACKUP_RETENTION_DAYS
#     (default 8, which leaves a margin around the 7-day policy boundary);
#   * the listing is read as structured JSON (rclone lsjson), never as newline-separated
#     text, and every object name must match the exact backup name format, so an object
#     whose name contains a newline/tab can neither inject a fake record nor reach the
#     delete list;
#   * preview and delete act on the same computed list: delete receives that list
#     verbatim through --files-from-raw, so nothing outside the preview can be removed;
#   * an empty or suspicious listing, an unknown modtime, a nested path or a listing
#     error aborts before any deletion.
#
# Default behaviour is a dry run: it lists what would go and deletes nothing.
#   bash scripts/prune_backups.sh                    # preview
#   bash scripts/prune_backups.sh --yes              # delete
#   bash scripts/prune_backups.sh --yes --retention-days 14
#
# Requires BACKUP_REMOTE and BACKUP_BUCKET (resolved by
# scripts/resolve_backup_storage.sh) plus rclone and python3. Only files matching
# newsbot-backup-<UTC timestamp>.dump directly under <remote>:<bucket>/db/ are considered;
# nothing else in the bucket is touched.
set -euo pipefail

APPLY=0
RETENTION="${BACKUP_RETENTION_DAYS:-8}"
MIN_KEEP="${BACKUP_MIN_KEEP:-3}"
PATTERN="newsbot-backup-*.dump"

# shellcheck disable=SC2016
usage() {
    cat >&2 <<'USAGE'
Usage: bash scripts/prune_backups.sh [--yes] [--retention-days N]

  --yes               actually delete (default: preview only)
  --retention-days N  delete dumps older than N days (default: 8)

Requires BACKUP_REMOTE and BACKUP_BUCKET. BACKUP_MIN_KEEP (default 3) newest
backups are always kept.
USAGE
}

while [ $# -gt 0 ]; do
    case "$1" in
        --yes)
            APPLY=1
            shift
            ;;
        --retention-days)
            if [ $# -lt 2 ]; then
                echo "ERROR: --retention-days needs a value." >&2
                usage
                exit 2
            fi
            RETENTION="$2"
            shift 2
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
for value_name in RETENTION MIN_KEEP; do
    value="${!value_name}"
    case "$value" in
        ""|*[!0-9]*)
            echo "ERROR: ${value_name} must be a positive integer, got '${value}'." >&2
            exit 1
            ;;
    esac
    if [ "$value" -lt 1 ]; then
        echo "ERROR: ${value_name} must be at least 1, got '${value}'." >&2
        exit 1
    fi
done

TARGET="${BACKUP_REMOTE}:${BACKUP_BUCKET}/db/"

json_file="$(mktemp)"
records_file="$(mktemp)"
list_file=""
trap 'rm -f "$json_file" "$records_file" "${list_file:-}"' EXIT

# 1. One authoritative, read-only listing in structured JSON form. A failed or partial
#    listing stops the script here, before any deletion.
if ! rclone lsjson "$TARGET" --files-only --include "$PATTERN" > "$json_file"; then
    echo "ERROR: could not list ${TARGET}; nothing was deleted." >&2
    exit 1
fi
if [ ! -s "$json_file" ]; then
    echo "ERROR: rclone returned an empty listing for ${TARGET}; nothing was deleted." >&2
    exit 1
fi

# 2. Validate every object in the JSON and emit epoch <TAB> modtime <TAB> size <TAB> name.
#    Only the exact names this project's backup script produces are accepted, so a name
#    containing a newline, tab or other control character aborts the run instead of being
#    split into extra records.
if ! python3 - "$json_file" > "$records_file" <<'PY'
import json
import re
import sys
from datetime import UTC, datetime

# backup.sh writes newsbot-backup-<date -u +%Y-%m-%d-%H%M%S>.dump
NAME = re.compile(r"newsbot-backup-[0-9]{4}-[0-9]{2}-[0-9]{2}-[0-9]{6}\.dump")
FLOOR = 978307200  # 2001-01-01: rclone's --default-time (2000-01-01) means "unknown"

try:
    objects = json.load(open(sys.argv[1], encoding="utf-8"))
except (OSError, ValueError) as exc:
    sys.exit(f"cannot parse the rclone listing: {exc}")
if not isinstance(objects, list):
    sys.exit("rclone listing was not a JSON array")

now = datetime.now(UTC).timestamp()
for obj in objects:
    if not isinstance(obj, dict):
        sys.exit("rclone listing contained a non-object entry")
    name = obj.get("Path") or obj.get("Name") or ""
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in name):
        sys.exit(f"object name contains a control character: {name!r}")
    if not NAME.fullmatch(name):
        sys.exit(f"object name is not a backup of this project: {name!r}")
    size = obj.get("Size")
    if not isinstance(size, int) or isinstance(size, bool) or size < 0:
        sys.exit(f"object {name!r} has an invalid size: {size!r}")
    modtime = obj.get("ModTime") or ""
    try:
        stamp = datetime.fromisoformat(re.sub(r"(\.\d{6})\d*", r"\1", modtime).replace("Z", "+00:00"))
    except ValueError:
        sys.exit(f"object {name!r} has an unparsable ModTime: {modtime!r}")
    epoch = stamp.timestamp()
    if epoch < FLOOR:
        sys.exit(f"object {name!r} has an implausible ModTime (unknown times appear as 2000-01-01): {modtime!r}")
    if epoch > now + 86400:
        sys.exit(f"object {name!r} has a ModTime in the future: {modtime!r}")
    print(f"{int(epoch)}\t{stamp.strftime('%Y-%m-%d %H:%M:%S')}\t{size}\t{name}")
PY
then
    echo "ERROR: the listing for ${TARGET} is not a clean set of project backups; nothing was deleted." >&2
    exit 1
fi

now="$(date -u +%s)"
rows=()
while IFS=$'\t' read -r mtime ts size path; do
    [ -z "${mtime}${ts}${size}${path}" ] && continue

    case "$ts" in
        [0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]\ [0-9][0-9]:[0-9][0-9]:[0-9][0-9])
            ;;
        *)
            echo "ERROR: unknown or invalid modtime '${ts}' for '${path}'; aborting before any deletion." >&2
            exit 1
            ;;
    esac
    case "$size" in
        ""|*[!0-9]*)
            echo "ERROR: invalid size '${size}' for '${path}'; aborting before any deletion." >&2
            exit 1
            ;;
    esac
    case "$path" in
        newsbot-backup-[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]-[0-9][0-9][0-9][0-9][0-9][0-9].dump)
            ;;
        *)
            echo "ERROR: unexpected file '${path}' under ${TARGET}; only ${PATTERN} is in scope. Aborting." >&2
            exit 1
            ;;
    esac

    rows+=("$(printf '%s\t%s\t%s\t%s' "$mtime" "$ts" "$size" "$path")")
done < "$records_file"

if [ "${#rows[@]}" -eq 0 ]; then
    echo "ERROR: no backups matching ${PATTERN} under ${TARGET}." >&2
    echo "Refusing to prune: an empty listing usually means the destination is wrong or empty." >&2
    exit 1
fi

# 3. Decide, newest first, using only that listing.
mapfile -t sorted < <(printf '%s\n' "${rows[@]}" | sort -t$'\t' -k1,1nr)

total="${#sorted[@]}"
cutoff_seconds=$((RETENTION * 86400))
delete_count=0
delete_bytes=0
delete_names=()

echo "--- Backups under ${TARGET} (${total} matching ${PATTERN}) ---"
for i in "${!sorted[@]}"; do
    IFS=$'\t' read -r mtime ts size path <<<"${sorted[$i]}"
    age=$((now - mtime))
    if [ "$i" -lt "$MIN_KEEP" ]; then
        reason="kept: newest ${MIN_KEEP}"
    elif [ "$age" -gt "$cutoff_seconds" ]; then
        reason="delete: older than ${RETENTION}d"
        delete_names+=("$path")
        delete_count=$((delete_count + 1))
        delete_bytes=$((delete_bytes + size))
    else
        reason="kept: younger than ${RETENTION}d"
    fi
    printf '  %-46s %s  %10s bytes  %s\n' "$path" "$ts" "$size" "$reason"
done

if [ "$delete_count" -eq 0 ]; then
    echo "Nothing to prune: ${total} backup(s) present, the newest ${MIN_KEEP} are always kept."
    exit 0
fi

echo "Eligible for deletion: ${delete_count} file(s), ${delete_bytes} bytes"
printf '  %s\n' "${delete_names[@]}"

if [ "$APPLY" -ne 1 ]; then
    echo "Dry run: nothing was deleted. Re-run with --yes to delete these files."
    exit 0
fi

# 4. Delete exactly the previewed names. --files-from-raw overrides every other filter,
#    so the list itself is the whole contract: each entry is re-validated here (exact
#    backup name, no path separators) and the temp file is removed on every exit path.
list_file="$(mktemp)" || {
    echo "ERROR: could not create the temporary delete list; nothing was deleted." >&2
    exit 1
}
{
    for name in "${delete_names[@]}"; do
        case "$name" in
            newsbot-backup-[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]-[0-9][0-9][0-9][0-9][0-9][0-9].dump)
                ;;
            *)
                echo "ERROR: refusing to write '${name}' to the delete list." >&2
                exit 1
                ;;
        esac
        printf '%s\n' "$name"
    done
} > "$list_file" || {
    echo "ERROR: could not write the temporary delete list; nothing was deleted." >&2
    exit 1
}
if [ ! -s "$list_file" ]; then
    echo "ERROR: the temporary delete list is empty; nothing was deleted." >&2
    exit 1
fi

echo "--- Deleting ${delete_count} file(s) (${delete_bytes} bytes) from ${TARGET} ---"
if ! rclone delete "$TARGET" --files-from-raw "$list_file"; then
    echo "ERROR: rclone delete failed; the listed backups may still exist." >&2
    exit 1
fi
echo "=== Prune completed ==="
