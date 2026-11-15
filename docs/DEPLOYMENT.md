# Deployment Guide (Serverless Zero-Cost Batch Architecture)

This guide covers deploying the Middle East News Bot using **GitHub Actions Cron workflows** and **free-tier managed services** ($0/month).

By switching from an always-on VM (Oracle/Freestyle) to scheduled batch execution in GitHub Actions, the deployment achieves:
- **Zero server management:** No VMs to provision, maintain, or keep alive.
- **Zero credit card requirement:** Neither GitHub Actions, Aiven, nor Backblaze B2 require a credit card.
- **100% cost-free:** Runs within the generous free-tier limits of GitHub Actions (2,000 public minutes/month) and managed free tiers.

---

## Architecture Overview

```
┌────────────────────────────────────────────────────────┐
│  GitHub Actions Scheduled Cron Runner                  │
│  .github/workflows/cron.yml (Runs Every 6h + 8:00 IRST) │
│  .github/workflows/backup.yml (Daily B2 Backup)        │
│  .github/workflows/keepalive.yml (Monthly Heartbeat)   │
└──────────────────────────┬─────────────────────────────┘
                           │
┌──────────────────────────▼─────────────────────────────┐
│  Database (Managed PostgreSQL 16 on Aiven)             │
│  Aiven Free: 1 CPU / 1GB RAM / 1GB Disk                │
└──────────────────────────┬─────────────────────────────┘
                           │
┌──────────────────────────▼─────────────────────────────┐
│  Backup & Disaster Recovery (Backblaze B2 via rclone)  │
│  Backblaze B2: 10GB Free Storage, Zero Egress Fees    │
└────────────────────────────────────────────────────────┘
```

---

## Free-Tier Provider Stack ($0/Month, No Credit Card)

| Component | Provider | Free Tier Specification | Card Required | Monthly Cost |
|---|---|---|---|---|
| **Compute & Scheduler** | **GitHub Actions** | 2,000 Linux runner minutes/month | ❌ No | **$0** |
| **Database** | **Aiven** | PostgreSQL 16 Free (1 CPU, 1GB RAM, 1GB disk) | ❌ No | **$0** |
| **Backup** | **Backblaze B2** | 10 GB storage, bucket-scoped app key, zero egress | ❌ No | **$0** |
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
| `MAX_ARTICLES_PER_BATCH` | `30` in `cron.yml` | Articles collected per feed and processed per run |
| `AI_MAX_ATTEMPTS` | `3` | Retry budget per failed article before it is left alone |
| `AI_MAX_CONSECUTIVE_FAILURES` | `5` | Circuit breaker: stop the batch after this many transient AI failures in a row |
| `IMMEDIATE_PUBLISH_ENABLED` | `true` | Publish each analyzed article to the channel right after it is stored |
| `IMMEDIATE_PUBLISH_ONLY_NEW_EVENTS` | `true` | Skip merged/duplicate events to avoid channel spam |
| `IMMEDIATE_PUBLISH_MAX_PER_RUN` | `10` | Cap on instant messages per batch; the rest wait for the daily report |
| `IMMEDIATE_PUBLISH_DELAY_SECONDS` | `2` | Pause between instant messages to avoid Telegram FloodWait |
| `POSTGRES_SSLROOTCERT` | empty | Path to a CA bundle (e.g. Aiven `ca.pem`); upgrades `sslmode=require` to real certificate validation |
| `NUMBER_CHECK_MODE` (repo variable `vars.NUMBER_CHECK_MODE`) | `log` | Numeric grounding of `headline_fa`/`summary_fa`: `off`, `log` (record in `ai_payload.number_check` only) or `flag` (park the article for review instead of publishing it) |
| `FLAG_ALERT_ENABLED` (repo variable `vars.FLAG_ALERT_ENABLED`) | `true` | Send one admin summary per run for the articles parked for review |
| `FLAG_ALERT_MAX_PER_RUN` (repo variable `vars.FLAG_ALERT_MAX_PER_RUN`) | `5` | Items listed in that single admin message; the rest appear as a count |

> `DRY_RUN_DAILY_REPORT=true` (workflow input) suppresses the daily report, the instant per-article messages **and** the flag alert; only `[DRY RUN]` blocks are printed into the logs. Use it for any test dispatch.

> These three are wired in `.github/workflows/cron.yml` as `${{ vars.X || '<default>' }}`, so the repository variable of the same name overrides the safe default (`log` / `true` / `5`) without editing the workflow. Set them under Settings → Secrets and variables → Actions → **Variables**; leave them unset to keep the defaults.

### Required Backup Secrets (for `backup.yml`)

| Secret Name | Description | Example Value |
|---|---|---|
| `B2_APPLICATION_KEY_ID` | Backblaze application key **ID** (rclone `account`) | `0051a2b3c4d5e6f0000000001` |
| `B2_APPLICATION_KEY` | Backblaze application key (rclone `key`) | `K005abcdefghijklmnopqrstuvwxyz` |
| `B2_BUCKET_NAME` | Bucket the key is scoped to | `newsbot-backups-jkiani89` |

> A Backblaze application key can only write to the bucket(s) it was issued for, so the
> workflow asks rclone which bucket the key can reach (`rclone lsd b2:`) and uses that name;
> `B2_BUCKET_NAME` is only the fallback. To use Cloudflare R2 instead, set the repository
> variable `BACKUP_STORAGE=r2` and provide `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`,
> `R2_ENDPOINT` (and optionally `R2_BUCKET_NAME`).

---

## 2. Workflows Explained

### 2.1 Batch Collection & Reporting (`.github/workflows/cron.yml`)
- **Triggers:**
  - `0 */6 * * *`: Every 6 hours (00:00, 06:00, 12:00, 18:00 UTC) for news collection and processing.
  - `30 4 * * *`: Daily at 04:30 UTC (= **08:00 AM Tehran Time**) to trigger the morning bulletin.
  - `30 16 * * *`: Daily at 16:30 UTC (= **20:00 Tehran Time**) for the evening bulletin.
  - `0 5`, `0 6`, `0 17`, `0 18` UTC: catch-up chances inside each slot's window (see below).
  - `workflow_dispatch`: Manual trigger from the GitHub Actions UI.

> **Catch-up window:** a slot is publishable for `SLOT_GRACE_HOURS` (default 3) after it
> starts, i.e. 08:00-11:00 and 20:00-23:00 Tehran. GitHub can delay or drop a scheduled run,
> so these extra runs make sure a late bulletin still ships; an already published slot is
> skipped by the record stored in `daily_reports.content.bulletins`.
- **Actions performed:**
  1. Sets up Python 3.15 and installs pre-compiled wheels.
  2. Runs `alembic upgrade head` on Aiven to keep schema updated.
  3. Executes `scripts/run_once.py`:
     - Synchronizes active feeds from `editorial/sources.json`.
     - Collects and analyzes new articles.
     - Publishes each analyzed article to the channel immediately after it is stored (capped per run, paced to avoid FloodWait, and skipped entirely in dry-run).
     - Checks whether a briefing slot is due or still inside its catch-up window; if so, generates and publishes that bulletin (with built-in deduplication so it never sends twice for the same slot).
     - Cleans up old request logs.

### 2.2 Repository Heartbeat (`.github/workflows/keepalive.yml`)
- **Trigger:** `0 3 1 * *` (monthly, 1st at 03:00 UTC) plus `workflow_dispatch`.
- **Why:** GitHub silently disables scheduled workflows in a **public** repository after 60 days
  without repository activity. This bot produces no commits of its own, so without a heartbeat
  the schedules would stop with no failed run and no email. The workflow stamps the date into
  `docs/HEARTBEAT.md` and commits it, well inside that window.
- **Manual re-enable:** if the schedule is ever disabled anyway
  ("This scheduled workflow is disabled because there hasn't been activity..."), open the
  Actions tab and click **Enable workflow**, or run `gh workflow enable cron.yml`.

### 2.3 Automated Database Backup (`.github/workflows/backup.yml`)
- **Triggers:**
  - `0 2 * * *`: Daily at 02:00 UTC.
  - `workflow_dispatch`: Manual trigger from GitHub Actions UI.
- **Actions performed:**
  1. Installs `rclone`.
  2. Uses transient `postgres:18-alpine` Docker container to run `pg_dump -Fc` against Aiven `DATABASE_URL` (the client must match the server's major version, otherwise `pg_dump` aborts).
  3. Uploads the compressed binary dump to Backblaze B2 and deletes backups older than 7 days.

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
| Backup fails with `rclone error` | Invalid B2 keys | Check `B2_APPLICATION_KEY_ID` and `B2_APPLICATION_KEY` in GitHub Secrets. |
| `you must use bucket "..." with this application key` | `B2_BUCKET_NAME` names a different bucket than the key allows | The workflow discovers the bucket from the key; correct the secret if you want it to be the fallback too. |
| `pg_dump: error: aborting because of server version mismatch` | Dump image older than the PostgreSQL server | Use `postgres:<server major>-alpine` in `scripts/backup.sh`. |

---

## 6. See Also

- [docs/BACKUP_RESTORE.md](file:///d:/Joud%20Files/middle-east-news-bot/docs/BACKUP_RESTORE.md) — Backup and disaster recovery procedures
- [docs/LOCAL_SETUP.md](file:///d:/Joud%20Files/middle-east-news-bot/docs/LOCAL_SETUP.md) — Local development guide
- [docs/archive/ANTI_IDLE.md](file:///d:/Joud%20Files/middle-east-news-bot/docs/archive/ANTI_IDLE.md) — Archived Oracle VM anti-idle guide
