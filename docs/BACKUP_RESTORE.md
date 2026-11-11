# Backup and Restore (PostgreSQL → Cloudflare R2)

This document explains how the Middle East News Bot backs up its PostgreSQL database to Cloudflare R2 and how to restore it.

## Why Cloudflare R2?

- **Zero egress fees:** Restoring backups does not cost money.
- **Free tier:** 10 GB storage, 1M write operations, 10M read operations per month.
- **S3-compatible:** Works natively with `rclone`.

## Prerequisites

1. **Cloudflare Account** with R2 enabled.
2. **R2 Bucket** created (default name: `newsbot-backups`).
3. **R2 API Token** (Access Key ID + Secret Access Key) with Object Read & Write permissions.
4. **rclone installed** on the host machine.

## 1. Install rclone on Oracle Cloud VM (Ubuntu/Debian)

```bash
sudo apt-get update
sudo apt-get install -y rclone
rclone version
```

## 2. Configure rclone for Cloudflare R2

Run `rclone config` and follow these steps:

1. Type `n` for "New remote".
2. Name: `r2`
3. Storage type: `s3` (Amazon S3 Compliant)
4. Provider: `Cloudflare`
5. env_auth: `false`
6. access_key_id: `<your_R2_Access_Key_ID>`
7. secret_access_key: `<your_R2_Secret_Access_Key>`
8. region: `auto`
9. endpoint: `https://<your_account_id>.r2.cloudflarestorage.com`
10. acl: `private`
11. Leave the rest as default.

Test the connection:

```bash
rclone lsd r2:
```

You should see your bucket(s) listed.

## 3. Run a Manual Backup

```bash
bash scripts/backup.sh
```

Check the R2 bucket to verify the `.dump` file was uploaded.

## 4. Automate Backups with Cron

Edit the crontab:

```bash
crontab -e
```

Add this line (runs daily at 02:00 AM server time):

```cron
0 2 * * * cd /path/to/middle-east-news-bot && bash scripts/backup.sh >> /var/log/newsbot-backup.log 2>&1
```

## 5. Restore from Backup

**WARNING:** Restore overwrites the current database (`--clean --if-exists`).

```bash
bash scripts/restore.sh newsbot-backup-2026-11-11-120000.dump
```

You will be prompted to type `yes` to confirm. The script will download the file from R2 if not present locally, restore it, and clean up.

## 6. Retention Policy

By default, backups older than 7 days are automatically deleted from R2. Change this by setting `BACKUP_RETENTION_DAYS` in `.env`.
