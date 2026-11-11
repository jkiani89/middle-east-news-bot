# Oracle Cloud Anti-Idle Strategy

Oracle Cloud reclaims Always Free VMs if **all three** conditions are met for **7 consecutive days**:
- CPU (95th percentile) < 20%
- Network < 20%
- Memory < 20%

**⚠️ Hourly cron jobs are NOT sufficient.** A cron that runs for 5 seconds per hour does not raise the 95th-percentile CPU above 20%.

## Solution: `scripts/keepalive.sh`

This script runs in the background on the VM. It allocates a fixed amount of RAM and performs light CPU work, keeping all metrics above the 20% threshold.

### How it works:
- **Memory:** Allocates ~2.5GB of RAM (≈21% of 12GB ARM VM).
- **CPU:** Burns 1 core at ~10% load continuously.
- **Network:** Generates a small HTTP request every 5 minutes to a public endpoint (optional, not included to keep it simple).

### Setup (on Oracle VM):

1. Make executable:
   ```bash
   chmod +x scripts/keepalive.sh
   ```

2. Run as a systemd service (recommended) or with nohup:
   ```bash
   nohup bash scripts/keepalive.sh > /var/log/newsbot-keepalive.log 2>&1 &
   ```
3. Verify:
   ```bash
   docker stats
   # You should see ~2.5GB of RAM used by the python process
   ```

## If Oracle VM is reclaimed

If the VM is accidentally reclaimed (e.g., due to a long period of downtime), restore from backup using `scripts/restore.sh` on a new Oracle VM or fallback to Freestyle.sh.
