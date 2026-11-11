#!/usr/bin/env bash
# Deploy script for Middle East News Bot on Docker Compose.
# Usage: bash scripts/deploy.sh
# Requires: git, docker, docker compose

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_ROOT"

echo "=== Deploy: $(date -u +'%Y-%m-%dT%H:%M:%SZ') ==="

# 1. Check prerequisites
if ! command -v docker >/dev/null 2>&1; then
    echo "ERROR: docker not found" >&2
    exit 1
fi
if ! docker compose version >/dev/null 2>&1; then
    echo "ERROR: docker compose (v2) not found" >&2
    exit 1
fi

# 2. Check .env exists
if [ ! -f .env ]; then
    echo "ERROR: .env not found. Copy .env.example to .env and fill values." >&2
    exit 1
fi

# 3. Pull latest code
echo "--- Pulling latest code ---"
git pull --ff-only

# 4. Build images
echo "--- Building images ---"
docker compose build --pull

# 5. Run migrations
echo "--- Running database migrations ---"
docker compose run --rm app alembic upgrade head

# 6. Start services
echo "--- Starting services ---"
docker compose up -d

# 7. Status
echo "--- Container status ---"
docker compose ps

echo "=== Deploy complete ==="
