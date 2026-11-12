#!/usr/bin/env bash
# Prevents Oracle Cloud Always Free VM from being reclaimed due to idle resources.
# Allocates ~2.5GB RAM and does light CPU work.
# Usage: nohup bash scripts/keepalive.sh &

set -euo pipefail

echo "=== Starting anti-idle keepalive ==="
echo "Allocating 2.5GB RAM and burning CPU..."

# Run a python script that holds memory and spins CPU
python3 -c '
import time
import multiprocessing

def hold_memory():
    # Allocate ~2.5GB (equivalent to ~20% of 12GB)
    a = bytearray(2 * 1024 * 1024 * 1024) 
    while True:
        for i in range(0, len(a), 4096):
            a[i] = 1
        time.sleep(0.1)

if __name__ == "__main__":
    hold_memory()
'
