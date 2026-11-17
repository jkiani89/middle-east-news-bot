# Backup and Restore (PostgreSQL → Backblaze B2)

This document explains how the Middle East News Bot backs up its PostgreSQL database to Backblaze B2 and how to restore it.

## Why Backblaze B2?

- **Zero egress fees:** Restoring backups does not cost money (up to 3× stored data per day).
- **Free tier:** 10 GB storage.
- **Native rclone support:** `type = b2`, no S3 compatibility layer needed.
- **Bucket-scoped application keys:** a key can only touch the bucket it was issued for.

> Cloudflare R2 is still supported by the same script: set `BACKUP_STORAGE=r2` and provide the
> `R2_*` secrets. B2 is the default in this project.

## Prerequisites

1. **Backblaze account** with a bucket created (this project uses `newsbot-backups-jkiani89`).
2. **Application Key** with access to that bucket, created in *App Keys*.
   You get an **application key ID** and an **application key**; the key ID is the `account`
   value in rclone, the key is the `key` value.
3. **rclone installed** on the host machine.

## 1. Install rclone

```bash
sudo apt-get update
sudo apt-get install -y rclone
rclone version
```

## 2. Configure rclone for Backblaze B2

Either run `rclone config` (New remote → name `b2` → storage type `b2` → paste the key ID and
key) or use environment variables, which is what the GitHub workflow does:

```bash
export RCLONE_CONFIG_B2_TYPE=b2
export RCLONE_CONFIG_B2_ACCOUNT="<B2_APPLICATION_KEY_ID>"
export RCLONE_CONFIG_B2_KEY="<B2_APPLICATION_KEY>"
```

Test the connection:

```bash
rclone lsd b2:
```

You should see the bucket the key is scoped to.

## 3. Run a Manual Backup

```bash
bash scripts/backup.sh
```

The dump is written by a `postgres:18-alpine` container, because `pg_dump` refuses to dump a
server newer than itself and the managed database runs PostgreSQL 18. The script validates
the dump with `pg_restore --list`, uploads it to `${BACKUP_REMOTE:-b2}:${BACKUP_BUCKET}/db/`,
and verifies the uploaded object's name and byte size. **It deletes nothing.**

## 4. Automate Backups

In this repository the backup is a GitHub Actions workflow
(`.github/workflows/backup.yml`, daily at 02:00 UTC), so nothing needs to run on a server.
For a self-hosted machine instead:

```cron
0 2 * * * cd /path/to/middle-east-news-bot && bash scripts/backup.sh >> /var/log/newsbot-backup.log 2>&1
```

## 5. Restore from Backup

**WARNING:** Restore overwrites the current database (`--clean --if-exists`).

```bash
bash scripts/restore.sh newsbot-backup-2026-11-14-090752.dump
```

You will be prompted to type `yes` to confirm. The script downloads the file from
`${BACKUP_REMOTE:-b2}:${BACKUP_BUCKET}/db/` if it is not already in `/tmp`, restores it with a
`postgres:18-alpine` `pg_restore`, and cleans up.

## 6. Retention Policy

Nothing is deleted automatically — not by `scripts/backup.sh`, not by
`.github/workflows/backup.yml`. Retention is an explicit, separate action so a backup run
can never destroy an earlier restore point:

```bash
# Preview what is older than 7 days (deletes nothing)
bash scripts/prune_backups.sh

# Delete dumps older than 7 days
bash scripts/prune_backups.sh --yes

# Different retention
bash scripts/prune_backups.sh --yes --retention-days 14
```

`BACKUP_REMOTE` and `BACKUP_BUCKET` must be set (run
`bash scripts/resolve_backup_storage.sh` first in Actions, or export the same variables
locally). Pruning is limited to `newsbot-backup-*.dump` under `<remote>:<bucket>/db/`, and
an empty or malformed destination aborts before any `rclone` call. `--retention-days`
defaults to `BACKUP_RETENTION_DAYS` (7).
