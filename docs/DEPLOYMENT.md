# Deployment Guide (Zero-Cost Architecture)

This guide covers deploying the Middle East News Bot on **free-tier infrastructure with zero credit card required** ($0/month). The architecture is designed to be **provider-agnostic**: all configuration is managed via `.env`, and switching components requires no application code changes.

## Architecture Overview

```
┌────────────────────────────────────────────────────────┐
│  Compute (Docker on Freestyle.sh VM)                   │
│  Freestyle.sh: 0.20 vCPU / 512MB RAM (Always-On)       │
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

**Portability:** Only `DATABASE_URL`, `GEMINI_API_KEY`, `TELEGRAM_*`, and `R2_*` are configured in `.env`. Application code and Dockerfile remain identical across environments.

---

## Free-Tier Provider Stack (No Credit Card Required)

| Component | Provider | Free Tier Specification | Card Required | Monthly Cost |
|---|---|---|---|---|
| **Compute** | **Freestyle.sh** | 200 vCPU-hours/month (0.20 vCPU = 146 vCPU-hours/month) | ❌ No | **$0** |
| **Database** | **Aiven** | PostgreSQL 16 Free (1 CPU, 1GB RAM, 1GB storage) | ❌ No | **$0** |
| **Backup** | **Cloudflare R2** | 10GB storage, 1M writes, 10M reads, zero egress fee | ❌ No | **$0** |
| **CI/CD** | **GitHub Actions** | 2,000 minutes/month (public repository) | ❌ No | **$0** |
| **AI Processing** | **Google Gemini** | Free Tier multi-project API pool (ISS-020) | ❌ No | **$0** |

> [!NOTE]
> The previous Oracle Cloud ARM deployment and anti-idle strategy have been archived to `docs/archive/ANTI_IDLE.md`. The Freestyle.sh + Aiven architecture removes any credit card requirement and eliminates the need for anti-idle CPU/RAM load scripts.

---

## 1. Database Setup: Aiven PostgreSQL 16 Free

### 1.1 Sign Up & Provisioning
1. Register at [aiven.io](https://aiven.io) (no credit card required).
2. Create a new service:
   - **Service:** PostgreSQL 16
   - **Plan:** Free Plan (1 CPU, 1GB RAM, 1GB disk storage)
   - **Cloud / Region:** Choose a region close to your target (e.g. `europe-west3` or `aws-eu-central-1`).
3. Once the service is running, find the **Connection Information** tab.

### 1.2 Connection String & Database URL
Copy the **Service URI** provided by Aiven, which looks like:
```text
postgres://avnadmin:PASSWORD@HOST:PORT/defaultdb?sslmode=require
```
Convert the scheme prefix from `postgres://` to `postgresql+asyncpg://` for SQLAlchemy async compatibility:
```text
postgresql+asyncpg://avnadmin:PASSWORD@HOST:PORT/defaultdb?sslmode=require
```
Set this value as `DATABASE_URL` in your `.env`.

### 1.3 Connection Pooling Guardrail (`max_connections = 20`)
Aiven Free enforces a strict limit of `max_connections = 20`.
- In `app/database/connection.py`, connection limits are configured to:
  ```python
  pool_size=5,
  max_overflow=5,
  ```
- This guarantees the application connection pool stays well under 20 connections.
- Do **not** use PgBouncer on Aiven Free (pooling add-on is disabled on the free tier). Keep bot worker concurrency within standard defaults.

### 1.4 Inactivity Power Off
Aiven may power down free database services during prolonged periods of zero queries. Because the bot performs hourly RSS collection and polling cycles, normal operation keeps the connection active. If the service is manually stopped or enters sleep:
- Simply click **Power On** in the Aiven web console to resume within ~60 seconds.

---

## 2. Compute Setup: Freestyle.sh VM

### 2.1 Sign Up & VM Creation
1. Sign up at [freestyle.sh](https://freestyle.sh) (no credit card required).
2. Create a new VM with:
   - **CPU:** `0.20 vCPU`
   - **RAM:** `512MB`
   - **Monthly quota calculation:** `0.20 vCPU × 730 hours = 146 vCPU-hours/month`, leaving a **27% safety margin** below the 200 vCPU-hour free ceiling.
3. **Crucial Configuration (Disable Sleep):**
   - Ensure the VM sleep setting is disabled in the Freestyle dashboard (or configure `sleep_after_ms` to a maximum value). Freestyle does not stop VMs by default unless sleep on idle is toggled on. Keep the VM continuously running so scheduled jobs fire on time.

### 2.2 Install Docker on Freestyle VM
Connect to your Freestyle shell or SSH and install Docker:
```bash
curl -fsSL https://get.docker.com | sh
```

Verify the installation:
```bash
docker --version
docker compose version
```

### 2.3 Clone & Configure Environment
```bash
git clone https://github.com/jkiani89/middle-east-news-bot.git
cd middle-east-news-bot

# Prepare environment file
cp .env.example .env
nano .env
```

Ensure the following variables are set in `.env`:
- `DATABASE_URL`: Your Aiven PostgreSQL connection URI (`postgresql+asyncpg://...`)
- `GEMINI_API_KEY`: One or more Google Gemini API keys
- `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHANNEL_ID`, `TELEGRAM_ADMIN_CHAT_ID`: Telegram credentials
- `R2_BUCKET_NAME`: Cloudflare R2 bucket name

### 2.4 Run Migrations & Deploy
Run deployment using the deployment script or Docker Compose:
```bash
# Apply Alembic schema migrations to Aiven
docker compose run --rm app alembic upgrade head

# Start application service in detached mode
docker compose up -d

# Verify container status
docker compose ps
docker compose logs -f app
```

---

## 3. Backup & Disaster Recovery: Cloudflare R2

Automated backups upload compressed binary dumps directly to Cloudflare R2 via `rclone`.

### 3.1 Setup rclone on the VM
Install `rclone`:
```bash
sudo apt-get update && sudo apt-get install -y rclone
```
Follow the configuration guide in [docs/BACKUP_RESTORE.md](file:///d:/Joud%20Files/middle-east-news-bot/docs/BACKUP_RESTORE.md) to set up remote `r2`.

### 3.2 Automated Daily Backup Cron
Open crontab:
```bash
crontab -e
```
Add daily backup at 02:00 AM server time:
```cron
0 2 * * * cd /root/middle-east-news-bot && bash scripts/backup.sh >> /var/log/newsbot-backup.log 2>&1
```

`scripts/backup.sh` automatically reads `DATABASE_URL`, runs a transient `postgres:16-alpine` container to execute `pg_dump -Fc` directly against Aiven, uploads to Cloudflare R2, and retains dumps for 7 days.

---

## 4. Operational Monitoring

The bot runs as a background worker (Telegram polling + APScheduler):

1. **Process Status:**
   ```bash
   docker compose ps
   ```
2. **Recent Logs:**
   ```bash
   docker compose logs --tail=100 -f app
   ```
3. **Freestyle Quota Monitoring:**
   - Periodically check the Freestyle dashboard to verify your vCPU-hour consumption remains under 200 hours.
4. **Heartbeat:**
   - In production, monitor errors by tailing the logs or checking notifications sent to `TELEGRAM_ADMIN_CHAT_ID`.

---

## 5. Troubleshooting Matrix

| Symptom | Cause | Solution |
|---|---|---|
| `too many clients already` | PostgreSQL connections exceeded | Check `app/database/connection.py`; ensure `pool_size=5, max_overflow=5`. |
| `connection refused` or timeout on DB | Aiven service powered off | Open Aiven console and click **Power On**. |
| `SSL connection has been closed unexpectedly` | Missing SSL parameter | Ensure `?sslmode=require` is present at the end of `DATABASE_URL`. |
| Container exits immediately | Invalid environment variable | Run `docker compose logs app` to inspect missing keys or validation errors. |
| Backup fails | R2 credentials or rclone remote missing | Run `rclone lsd r2:` to test connectivity. |

---

## 6. See Also

- [docs/BACKUP_RESTORE.md](file:///d:/Joud%20Files/middle-east-news-bot/docs/BACKUP_RESTORE.md) — Backup and restore workflows
- [docs/LOCAL_SETUP.md](file:///d:/Joud%20Files/middle-east-news-bot/docs/LOCAL_SETUP.md) — Local development guidelines
- [docs/archive/ANTI_IDLE.md](file:///d:/Joud%20Files/middle-east-news-bot/docs/archive/ANTI_IDLE.md) — Archived Oracle Cloud anti-idle guide
