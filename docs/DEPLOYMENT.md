# Deployment Guide (Serverless Zero-Cost Batch Architecture)

This guide covers deploying the Middle East News Bot using **GitHub Actions Cron workflows** and **free-tier managed services** ($0/month).

By switching from an always-on VM (Oracle/Freestyle) to scheduled batch execution in GitHub Actions, the deployment achieves:
- **Zero server management:** No VMs to provision, maintain, or keep alive.
- **Zero credit card requirement:** Neither GitHub Actions, Aiven, nor Cloudflare R2 require a credit card.
- **100% cost-free:** Runs within the generous free-tier limits of GitHub Actions (2,000 public minutes/month) and managed free tiers.

---

## Architecture Overview

```
┌────────────────────────────────────────────────────────┐
│  GitHub Actions Scheduled Cron Runner                  │
│  .github/workflows/cron.yml (Runs Every 6h + 8:00 IRST) │
│  .github/workflows/backup.yml (Daily R2 Backup)        │
└──────────────────────────┬─────────────────────────────┘
                           │
┌──────────────────────────▼─────────────────────────────┐
│  Database (Managed PostgreSQL 16 on Aiven)             │
│  Aiven Free: 1 CPU / 1GB RAM / 1GB Disk                │
└──────────────────────────┬─────────────────────────────┘
                           │
┌──────────────────────────▼─────────────────────────────┐
│  Backup & Disaster Recovery (Cloudflare R2 via rclone) │
│  Cloudflare R2: 10GB Free Storage, Zero Egress Fees   │
└────────────────────────────────────────────────────────┘
```

---

## Free-Tier Provider Stack ($0/Month, No Credit Card)

| Component | Provider | Free Tier Specification | Card Required | Monthly Cost |
|---|---|---|---|---|
| **Compute & Scheduler** | **GitHub Actions** | 2,000 Linux runner minutes/month | ❌ No | **$0** |
| **Database** | **Aiven** | PostgreSQL 16 Free (1 CPU, 1GB RAM, 1GB disk) | ❌ No | **$0** |
| **Backup** | **Cloudflare R2** | 10GB storage, 1M writes, 10M reads, zero egress | ❌ No | **$0** |
| **AI Processing** | **Google Gemini** | Free Tier multi-project API pool (ISS-020) | ❌ No | **$0** |
| **Delivery** | **Telegram Bot API** | Bot and Channel broadcast | ❌ No | **$0** |

---

## 1. Step-by-Step GitHub Actions Secrets Setup

To run the workflows securely, add your credentials as **Repository Secrets**:

1. On your GitHub repository page, navigate to:
   **Settings** → **Secrets and variables** → **Actions**
2. Click **New repository secret** for each variable below:

### Required Application Secrets

| Secret Name | Description | Example Value |
|---|---|---|
| `DATABASE_URL` | Aiven PostgreSQL connection URI with `postgresql+asyncpg://` | `postgresql+asyncpg://avnadmin:pass@host:port/defaultdb?sslmode=require` |
| `GEMINI_API_KEY` | One or more Google Gemini API keys (comma-separated) | `AIzaSyA..., AIzaSyB...` |
| `TELEGRAM_BOT_TOKEN` | Bot API token from @BotFather | `123456789:ABCdefGhIJKlmNoPQRsTUVwxyZ` |
| `TELEGRAM_CHANNEL_ID` | Telegram Channel ID (must start with `-100`) | `-1001234567890` |
| `TELEGRAM_ADMIN_CHAT_ID` | Your numeric Telegram user ID for admin alerts | `987654321` |

### Optional Tuning Variables

Set these as **Repository Variables** (Settings → Secrets and variables → Actions → Variables) or leave them at their defaults:

| Variable | Default | Description |
|---|---|---|
| `GEMINI_PRIMARY_MODEL` | `gemini-3.8-flash` | Primary analysis model |
| `GEMINI_FALLBACK_MODEL` | `gemini-3.5-flash-lite` | Fallback model used on 429/503 |
| `GEMINI_ENABLE_FALLBACK` | `true` | Enable the fallback model |
| `MAX_ARTICLES_PER_BATCH` | `15` in `cron.yml` | Articles collected per feed and processed per run |
| `AI_MAX_ATTEMPTS` | `3` | Retry budget per failed article before it is left alone |
| `AI_MAX_CONSECUTIVE_FAILURES` | `5` | Circuit breaker: stop the batch after this many transient AI failures in a row |
| `IMMEDIATE_PUBLISH_ENABLED` | `true` | Publish each analyzed article to the channel right after it is stored |
| `IMMEDIATE_PUBLISH_ONLY_NEW_EVENTS` | `true` | Skip merged/duplicate events to avoid channel spam |
| `IMMEDIATE_PUBLISH_MAX_PER_RUN` | `10` | Cap on instant messages per batch; the rest wait for the daily report |
| `IMMEDIATE_PUBLISH_DELAY_SECONDS` | `2` | Pause between instant messages to avoid Telegram FloodWait |
| `POSTGRES_SSLROOTCERT` | empty | Path to a CA bundle (e.g. Aiven `ca.pem`); upgrades `sslmode=require` to real certificate validation |

> `DRY_RUN_DAILY_REPORT=true` (workflow input) suppresses **both** the daily report and the instant per-article messages, and only prints `[DRY RUN]` blocks into the logs. Use it for any test dispatch.

### Required Backup Secrets (for `backup.yml`)

| Secret Name | Description | Example Value |
|---|---|---|
| `R2_ACCESS_KEY_ID` | Cloudflare R2 Access Key ID | `a1b2c3d4e5...` |
| `R2_SECRET_ACCESS_KEY` | Cloudflare R2 Secret Access Key | `f6g7h8i9j0...` |
| `R2_ENDPOINT` | Cloudflare R2 S3 API Endpoint | `https://<account_id>.r2.cloudflarestorage.com` |
| `R2_BUCKET_NAME` | R2 Bucket Name (optional, defaults to `newsbot-backups`) | `newsbot-backups` |

---

## 2. Workflows Explained

### 2.1 Batch Collection & Reporting (`.github/workflows/cron.yml`)
- **Triggers:**
  - `0 */6 * * *`: Every 6 hours (00:00, 06:00, 12:00, 18:00 UTC) for news collection and processing.
  - `30 4 * * *`: Daily at 04:30 UTC (= **08:00 AM Tehran Time**) to trigger the morning intelligence report.
  - `workflow_dispatch`: Manual trigger from the GitHub Actions UI.
- **Actions performed:**
  1. Sets up Python 3.15.2 and installs pre-compiled wheels.
  2. Runs `alembic upgrade head` on Aiven to keep schema updated.
  3. Executes `scripts/run_once.py`:
     - Synchronizes active feeds from `editorial/sources.json`.
     - Collects and analyzes new articles.
     - Publishes each analyzed article to the channel immediately after it is stored (capped per run, paced to avoid FloodWait, and skipped entirely in dry-run).
     - Checks if current time is between 8:00 - 9:00 AM Tehran; if so, generates and publishes the daily report (with built-in deduplication so it never sends twice on the same day).
     - Cleans up old request logs.

### 2.2 Automated Database Backup (`.github/workflows/backup.yml`)
- **Triggers:**
  - `0 2 * * *`: Daily at 02:00 UTC.
  - `workflow_dispatch`: Manual trigger from GitHub Actions UI.
- **Actions performed:**
  1. Installs `rclone`.
  2. Uses transient `postgres:16-alpine` Docker container to run `pg_dump -Fc` against Aiven `DATABASE_URL`.
  3. Uploads the compressed binary dump to Cloudflare R2 and deletes backups older than 7 days.

---

## 3. Database Setup: Aiven PostgreSQL 16 Free

1. Sign up at [aiven.io](https://aiven.io) (no credit card required).
2. Create a service: **PostgreSQL 16**, plan: **Free Plan** (1 CPU, 1GB RAM, 1GB storage).
3. From the service overview, copy the **Service URI**:
   ```text
   postgres://avnadmin:PASSWORD@HOST:PORT/defaultdb?sslmode=require
   ```
4. Convert the prefix to `postgresql+asyncpg://`:
   ```text
   postgresql+asyncpg://avnadmin:PASSWORD@HOST:PORT/defaultdb?sslmode=require
   ```
5. Save this URI as `DATABASE_URL` in your GitHub Repository Secrets.

> [!TIP]
> **Inactivity Keepalive:** Aiven may sleep free databases after long inactivity. Because GitHub Actions runs every 6 hours and queries the database, the connection is kept active automatically.

---

## 4. Manual Testing & Verification

You can test the entire pipeline on demand without waiting for the cron timer:

1. In GitHub, click the **Actions** tab.
2. Select **Scheduled Batch Runner** in the left sidebar.
3. Click **Run workflow**:
   - Check `dry_run_daily_report` if you want to test report generation without messaging Telegram.
   - Check `force_daily_report` to generate and send a report regardless of the time window.
4. Inspect the workflow run logs to confirm successful execution.

### Local Testing with Python
To run the batch script locally with your `.env`:
```powershell
.\.venv\Scripts\python scripts/run_once.py --dry-run-daily-report
```

---

## 5. Troubleshooting Matrix

| Symptom | Cause | Solution |
|---|---|---|
| `FATAL: password authentication failed` | Incorrect Aiven credentials | Verify `DATABASE_URL` in GitHub Secrets. |
| Workflow fails on `alembic upgrade` | Missing SSL flag in connection string | Ensure `?sslmode=require` is at the end of `DATABASE_URL`. |
| Daily report not sent in morning | Iran timezone mismatch | Iran time is UTC+3:30; `cron: '30 4 * * *'` matches 08:00 AM Tehran. |
| Telegram message rejected | Bot not admin in channel | Add bot to your channel and grant "Post Messages" permission. |
| Backup fails with `rclone error` | Invalid R2 keys | Check `R2_ACCESS_KEY_ID` and `R2_SECRET_ACCESS_KEY` in GitHub Secrets. |

---

## 6. See Also

- [docs/BACKUP_RESTORE.md](file:///d:/Joud%20Files/middle-east-news-bot/docs/BACKUP_RESTORE.md) — Backup and disaster recovery procedures
- [docs/LOCAL_SETUP.md](file:///d:/Joud%20Files/middle-east-news-bot/docs/LOCAL_SETUP.md) — Local development guide
- [docs/archive/ANTI_IDLE.md](file:///d:/Joud%20Files/middle-east-news-bot/docs/archive/ANTI_IDLE.md) — Archived Oracle VM anti-idle guide
