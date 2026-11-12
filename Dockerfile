# syntax=docker/dockerfile:1.7
# Multi-stage Dockerfile for Middle East News Bot
# Builder installs Python deps from pre-built wheels (all ARM64-compatible).
# Runtime is a slim Debian image with tzdata only.

FROM python:3.15.2-slim AS builder
WORKDIR /build

# Install dependencies from requirements.txt into /install
COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install --no-deps 'aiogram==3.22.0' \
    && pip install --no-cache-dir --prefix=/install -r requirements.txt


FROM python:3.15.2-slim AS runtime

# tzdata is required for ZoneInfo('Asia/Tehran')
RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

# Copy installed Python packages from builder
COPY --from=builder /install /usr/local

# Create non-root user
RUN useradd --create-home --shell /bin/bash --uid 1000 appuser

# Application code (owned by appuser, not root)
WORKDIR /app
COPY --chown=appuser:appuser app/ ./app/
COPY --chown=appuser:appuser alembic/ ./alembic/
COPY --chown=appuser:appuser alembic.ini .
COPY --chown=appuser:appuser requirements.txt .
# editorial/sources.json is required at startup by app.collectors.source_manager
COPY --chown=appuser:appuser editorial/ ./editorial/

# Python runtime settings
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

USER appuser

# Default command (overridden by docker-compose for migrations)
CMD ["python", "-m", "app.main"]
