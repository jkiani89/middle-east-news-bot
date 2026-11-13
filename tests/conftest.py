"""Pytest configuration and fixtures for Middle East News Bot."""

import os
import subprocess
import sys
from pathlib import Path

import psycopg
import pytest

# Ensure safe test environment defaults before any app import
os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://newsbot:newsbot_local_pass@127.0.0.1:5432/newsbot_test")
os.environ.setdefault("GEMINI_API_KEY", "test_gemini_key_12345")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123456789:TEST_BOT_TOKEN_XYZ")
os.environ.setdefault("TELEGRAM_CHANNEL_ID", "-1009876543210")
os.environ.setdefault("TELEGRAM_ADMIN_CHAT_ID", "987654321")


@pytest.fixture(autouse=True)
def _disable_live_article_extraction(monkeypatch):
    """Keep the suite offline: article bodies are extracted lazily in the pipeline now.

    Individual tests that exercise hydration patch this function themselves.
    """

    async def _no_body(*args, **kwargs):
        return None

    monkeypatch.setattr("app.processors.pipeline.fetch_article_full_text", _no_body)


@pytest.fixture(scope="session")
def sync_test_dsn() -> str:
    """Sync DSN for test database (psycopg format, no +asyncpg)."""
    dsn = os.environ.get(
        "DATABASE_URL",
        "postgresql+asyncpg://newsbot:newsbot_local_pass@127.0.0.1:5432/newsbot_test",
    )
    return dsn.replace("postgresql+asyncpg://", "postgresql://")


@pytest.fixture(scope="session")
def ensure_test_schema(sync_test_dsn: str):
    """Ensure test DB has all tables (including gemini_*).

    Runs alembic upgrade head on newsbot_test if gemini tables don't exist.
    Skips all dependent tests if PostgreSQL is unreachable.
    """
    try:
        with psycopg.connect(sync_test_dsn, autocommit=True) as conn:
            cur = conn.execute("SELECT to_regclass('public.gemini_suspended_keys')")
            row = cur.fetchone()
            exists = row is not None and row[0] is not None
    except psycopg.OperationalError as exc:
        pytest.skip(f"PostgreSQL not reachable: {exc}")

    if not exists:
        env = os.environ.copy()
        env["DATABASE_URL"] = sync_test_dsn.replace("postgresql://", "postgresql+asyncpg://")
        result = subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            env=env,
            capture_output=True,
            text=True,
            cwd=Path(__file__).parent.parent,
            check=False,
        )
        if result.returncode != 0:
            pytest.skip(f"Alembic upgrade failed: {result.stderr}")
    yield


@pytest.fixture
def postgres_adapter(sync_test_dsn: str, ensure_test_schema):
    """PostgresAdapter with clean slate (TRUNCATE before each test)."""
    from app.ai.postgres_adapter import PostgresAdapter

    adapter = PostgresAdapter(sync_test_dsn)
    adapter._ensure().execute(
        "TRUNCATE gemini_suspended_keys, gemini_key_stats, gemini_request_log"
    )
    yield adapter
    adapter.close()
