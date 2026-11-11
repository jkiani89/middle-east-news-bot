# Deployment Guide (Zero-Cost Architecture)

This guide covers deploying the Middle East News Bot on **free-tier infrastructure only** ($0/month). The architecture is designed to be **provider-agnostic**: all configuration is via `.env`, and switching providers requires no code changes.

## Architecture Overview

```
┌─────────────────────────────────────────────────────┐
│  Compute (Docker)                                   │
│  Primary:  Oracle Cloud Always Free ARM VM          │
│  Fallback: Freestyle.sh (0.20 vCPU)                 │
└──────────────────────┬──────────────────────────────┘
                       │
┌──────────────────────▼──────────────────────────────┐
│  Database (PostgreSQL 16)                           │
│  Primary:  Self-hosted on Oracle VM (Docker)        │
│  Fallback: Aiven Free PostgreSQL                    │
└──────────────────────┬──────────────────────────────┘
                       │
┌──────────────────────▼──────────────────────────────┐
│  Backup (S3-compatible)                             │
│  Primary:  Cloudflare R2 (10GB, zero egress)        │
└─────────────────────────────────────────────────────┘
```

**Portability:** Only `DATABASE_URL`, `GEMINI_API_KEY`, `TELEGRAM_*`, and `R2_*` change between providers. All application code and Dockerfiles remain identical.

---

## Free-Tier Provider Matrix (as of 2026-11)

| Provider | Free Tier | Always-On | Card Required | Recommended |
|---|---|---|---|---|
| **Oracle Cloud Always Free (ARM)** | 2 OCPU / 12GB RAM / 200GB disk / 10TB egress | ✅ (not idle) | ✅ (verify only) | ⭐ **Primary** |
| **Freestyle.sh** | 200 vCPU-hour/month (0.20 vCPU = ~146 hours) | ⚠️ paused after quota | ❌ | ⭐ Fallback |
| Railway | $1 credit/month (≈3-5 days always-on) | ❌ | ✅ | ❌ |
| Render Free | 512MB, sleeps after 15min inactivity | ❌ | ✅ | ❌ |
| Fly.io | No free tier for new accounts (Oct 2024) | — | — | ❌ |
| Cloudflare Workers | Python async + asyncpg unsupported | — | — | ❌ |

**Why not serverless?** This bot requires:
- **aiogram polling** — a persistent process (not request-response)
- **APScheduler** — persistent cron (not serverless schedules)
- **asyncpg + PostgreSQL** — not available on Cloudflare Workers

Therefore a **long-running VM or container** is required.

---

## Step 1: Provision Oracle Cloud ARM VM (Primary)

### 1.1 Sign up
1. Go to https://cloud.oracle.com/free
2. Sign up with email + phone (credit card required for identity verification, **never charged on Always Free**)
3. Choose home region (pick one close to your users — e.g., `eu-frankfurt-1` or `me-jeddah-1`)

### 1.2 Create ARM VM
1. Navigate: **Compute → Instances → Create Instance**
2. **Name:** `newsbot-prod`
3. **Image:** Ubuntu 24.04 LTS (ARM)
4. **Shape:** `VM.Standard.A1.Flex` — **2 OCPU, 12GB RAM** (maximum Always Free)
5. **Boot volume:** 100GB (out of 200GB total free)
6. **SSH key:** Upload your public key (or generate new — save private key)
7. **Networking:** Assign public IPv4 (default)
8. Click **Create**

Wait ~1 minute. Copy the **Public IP address**.

### 1.3 Connect via SSH

```bash
ssh -i /path/to/private_key ubuntu@<PUBLIC_IP>
```

### 1.4 Install Docker + Docker Compose

```bash
# Install Docker
curl -fsSL https://get.docker.com | sudo sh

# Add ubuntu user to docker group
sudo usermod -aG docker ubuntu

# Logout and login for group change to take effect
exit
# SSH back in

# Verify
docker --version
docker compose version
```

### 1.5 Install rclone (for backup)

```bash
sudo apt-get update
sudo apt-get install -y rclone
rclone --version
```

Configure rclone for Cloudflare R2 — see `docs/BACKUP_RESTORE.md`.

### 1.6 Open PostgreSQL port for backups (optional)

PostgreSQL is bound to `127.0.0.1:5432` (internal only). For remote backups from local, you can use SSH tunnel. **Not required** if backups run on the VM itself via cron.

### 1.7 Clone and deploy

```bash
# Clone
cd ~
git clone https://github.com/YOUR_USERNAME/middle-east-news-bot.git
cd middle-east-news-bot

# Configure
cp .env.example .env
nano .env
# Fill in: DATABASE_URL, POSTGRES_PASSWORD, GEMINI_API_KEY, TELEGRAM_*,
# R2_BUCKET_NAME (and set rclone per BACKUP_RESTORE.md)

# Deploy
bash scripts/deploy.sh
```

Verify:
```bash
docker compose ps    # Both containers should be "Up"
docker compose logs -f app    # Tail logs
```

---

## Step 2: Configure Backup Cron

Edit crontab:
```bash
crontab -e
```

Add:
```cron
# Daily backup at 02:00 UTC
0 2 * * * cd /home/ubuntu/middle-east-news-bot && bash scripts/backup.sh >> /var/log/newsbot-backup.log 2>&1
```

See `docs/BACKUP_RESTORE.md` for restore procedure.

---

## Step 3: Anti-Idle Strategy (Critical for Oracle)

Oracle reclaims Always Free VMs if **ALL three** conditions are met for **7 consecutive days**:
- CPU (95th percentile) < 20%
- Network < 20%
- Memory < 20%

**⚠️ A cron job alone is not enough.** A cron that runs 5 seconds/hour does not raise the 95th percentile CPU.

See **`docs/ANTI_IDLE.md`** for the anti-idle strategy and script.

---

## Step 4: Freestyle.sh (Fallback Compute)

If Oracle VM is unavailable (network, hardware, or accidental reclaim), deploy to Freestyle.sh.

### 4.1 Free tier limits
- 200 vCPU-hour/month
- At 0.20 vCPU always-on: **146 vCPU-hours/month** (27% safety margin)
- VM **pauses** when quota exhausted (until next month)

### 4.2 Setup
1. Sign up at https://freestyle.sh
2. Create new project → connect GitHub repo `YOUR_USERNAME/middle-east-news-bot`
3. **Resources:** 0.20 vCPU, 512MB RAM (do not exceed — will use more quota)
4. **Command:** `bash scripts/deploy.sh` (or `docker compose up -d`)
5. **Environment variables:** Copy from `.env` (all required vars)

### 4.3 Database for Freestyle

Use **Aiven Free PostgreSQL** (Freestyle's ephemeral storage is not suitable):
1. Sign up at https://aiven.io (no credit card)
2. Create PostgreSQL 16 service (free tier: 1 CPU, 1GB RAM, 1GB disk)
3. Copy **Service URI** → set as `DATABASE_URL` in Freestyle (convert `postgres://` to `postgresql+asyncpg://`)
4. Run `alembic upgrade head` once (Freestyle → Shell)
5. Note: Aiven free tier **powers off after inactivity** — with active polling it stays up

### 4.4 Backup with Aiven
Cloudflare R2 backup still works. `scripts/backup.sh` needs `POSTGRES_USER`, `POSTGRES_DB` from Aiven connection string.

---

## Step 5: Aiven PostgreSQL (Fallback DB)

If Oracle VM is down but you need the DB online immediately:

1. **Backup current DB** (from VM): `bash scripts/backup.sh`
2. **Create Aiven service** (see 4.3)
3. **Restore to Aiven:**
   ```bash
   # Point DATABASE_URL to Aiven, then:
   alembic upgrade head
   pg_restore --dbname="<AIVEN_URI>" --clean --if-exists /tmp/newsbot-backup-*.dump
   ```
4. Update `DATABASE_URL` in `.env` (Oracle) or Freestyle env (if using Freestyle)

**Migration takes ~10 minutes. Plan downtime accordingly.**

---

## Step 6: Cloudflare R2 (Backups)

See `docs/BACKUP_RESTORE.md`. Setup is independent of compute provider.

---

## Monitoring

The bot does not expose an HTTP endpoint (worker model). Health monitoring options:

1. **Process check** (Oracle VM):
   ```bash
   docker compose ps | grep -q "Up.*men-app" && echo OK || echo FAIL
   ```
2. **Log check** (daily):
   ```bash
   docker compose logs --since 24h app | grep -c "ERROR"
   ```
3. **Telegram heartbeat** (recommended): Add a scheduled job that sends a "still alive" message to `TELEGRAM_ADMIN_CHAT_ID` once per day. See `app/scheduler/jobs.py`.

---

## Migration Between Providers

The architecture is provider-agnostic. To move from one provider to another:

1. **Stop current deployment:** `docker compose down`
2. **Backup DB:** `bash scripts/backup.sh`
3. **Restore on new provider:** follow Step 5 above
4. **Update `.env`:**
   - If compute changed: clone repo, copy `.env`, `bash scripts/deploy.sh`
   - If DB changed: update `DATABASE_URL` (host only — schema is portable)
5. **Verify:** `docker compose logs app` — no errors within 5 minutes
6. **DNS/domain:** N/A (bot has no incoming webhook — outbound polling only)

**Zero code changes required** for any provider switch.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `docker compose up` hangs on postgres | Volume permissions | `sudo chown -R 999:999 ./data/pgdata` |
| `alembic upgrade head` fails | DATABASE_URL points to old DB | Check `.env` → recreate stack |
| Bot exits immediately | Missing GEMINI_API_KEY or TELEGRAM_* | Check `docker compose logs app` |
| Backup uploads fail | rclone not configured or R2 credentials wrong | Test `rclone lsd r2:` |
| Oracle VM slow / OOM | Memory leak or 12GB exhausted | Check `docker stats` |

---

## Cost Summary

| Component | Monthly Cost |
|---|---|
| Oracle Cloud ARM VM (Always Free) | **$0** |
| Self-hosted PostgreSQL (in same VM) | **$0** |
| Cloudflare R2 (10GB free tier) | **$0** |
| GitHub Actions (public repo) | **$0** |
| Gemini API (free tier quotas) | **$0** (with rate limit awareness) |
| **Total** | **$0/month** |

**Only risk:** Gemini free tier quota exhaustion (see `docs/MODEL_QUOTAS.md`). If free tier is exhausted, bot skips processing until quota resets (hourly or daily). No unexpected charges.

---

## See Also

- `docs/BACKUP_RESTORE.md` — Backup configuration
- `docs/ANTI_IDLE.md` — Oracle anti-idle strategy
- `docs/LOCAL_SETUP.md` — Local development setup
- `docs/MODEL_QUOTAS.md` — Gemini quota management
