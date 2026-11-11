"""Integration tests for PostgresAdapter using real PostgreSQL test database."""

import pytest
from gemini_rotator.adapters import DBAdapter
from gemini_rotator.models import KeyStats

from app.ai.postgres_adapter import PostgresAdapter
from app.ai.provider import GeminiProvider


def test_adapter_implements_db_adapter(postgres_adapter: PostgresAdapter) -> None:
    """Verify PostgresAdapter satisfies DBAdapter protocol at runtime."""
    assert isinstance(postgres_adapter, DBAdapter)
    assert issubclass(PostgresAdapter, DBAdapter)


def test_mark_and_get_suspended_keys(postgres_adapter: PostgresAdapter) -> None:
    """Verify mark_key_suspended persists keys and get_suspended_keys returns them."""
    postgres_adapter.mark_key_suspended("k1", 12345.67)
    result = postgres_adapter.get_suspended_keys()
    assert result == {"k1"}

    cur = postgres_adapter._ensure().execute(
        "SELECT key, suspended_at FROM gemini_suspended_keys WHERE key = %s",
        ("k1",),
    )
    row = cur.fetchone()
    assert row is not None
    assert row["key"] == "k1"
    assert row["suspended_at"] == pytest.approx(12345.67)


def test_mark_key_is_idempotent(postgres_adapter: PostgresAdapter) -> None:
    """Verify repeatedly marking key updates timestamp without duplicate key error."""
    postgres_adapter.mark_key_suspended("k1", 1000.0)
    postgres_adapter.mark_key_suspended("k1", 2000.0)
    assert postgres_adapter.get_suspended_keys() == {"k1"}

    cur = postgres_adapter._ensure().execute(
        "SELECT suspended_at FROM gemini_suspended_keys WHERE key = %s",
        ("k1",),
    )
    row = cur.fetchone()
    assert row is not None
    assert row["suspended_at"] == pytest.approx(2000.0)


def test_unmark_key(postgres_adapter: PostgresAdapter) -> None:
    """Verify unmark_key_suspended removes key from suspended set."""
    postgres_adapter.mark_key_suspended("k1", 1000.0)
    assert "k1" in postgres_adapter.get_suspended_keys()

    postgres_adapter.unmark_key_suspended("k1")
    assert postgres_adapter.get_suspended_keys() == set()


def test_unmark_unknown_key_no_error(postgres_adapter: PostgresAdapter) -> None:
    """Verify unmarking a key that does not exist succeeds silently."""
    postgres_adapter.unmark_key_suspended("nonexistent")
    assert postgres_adapter.get_suspended_keys() == set()


def test_record_request_writes_to_log(postgres_adapter: PostgresAdapter) -> None:
    """Verify record_request writes detailed telemetry into gemini_request_log."""
    postgres_adapter.record_request(
        "k1",
        "success",
        latency=0.0425,
        model="gemini-3.8-flash",
    )

    cur = postgres_adapter._ensure().execute("SELECT count(*) FROM gemini_request_log")
    count_row = cur.fetchone()
    assert count_row is not None
    assert count_row["count"] == 1

    cur = postgres_adapter._ensure().execute(
        "SELECT key, event, latency_ms, model FROM gemini_request_log",
    )
    row = cur.fetchone()
    assert row is not None
    assert row["key"] == "k1"
    assert row["event"] == "success"
    assert row["latency_ms"] == pytest.approx(42.5)
    assert row["model"] == "gemini-3.8-flash"

    # Also check latency history query
    history = postgres_adapter.get_latency_history(key="k1", model="gemini-3.8-flash")
    assert len(history) == 1
    assert history[0]["key"] == "k1"
    assert history[0]["latency_ms"] == pytest.approx(42.5)


def test_record_request_null_latency_and_model(postgres_adapter: PostgresAdapter) -> None:
    """Verify record_request handles error events with None latency and model."""
    postgres_adapter.record_request("k1", "error")
    cur = postgres_adapter._ensure().execute(
        "SELECT latency_ms, model FROM gemini_request_log",
    )
    row = cur.fetchone()
    assert row is not None
    assert row["latency_ms"] is None
    assert row["model"] is None


def test_multiple_records_accumulate(postgres_adapter: PostgresAdapter) -> None:
    """Verify multiple record_request calls accumulate rows in log table."""
    for i in range(3):
        postgres_adapter.record_request(f"k{i}", "success")

    cur = postgres_adapter._ensure().execute("SELECT count(*) FROM gemini_request_log")
    count_row = cur.fetchone()
    assert count_row is not None
    assert count_row["count"] == 3


def test_get_stats_returns_empty_stats_for_unknown(postgres_adapter: PostgresAdapter) -> None:
    """Verify get_stats returns default KeyStats object when key has no recorded history."""
    stats = postgres_adapter.get_stats("unknown")
    assert stats is not None
    assert isinstance(stats, KeyStats)
    assert stats.key == "unknown"
    assert stats.total_requests == 0
    assert stats.total_success == 0


def test_get_stats_after_successful_request(postgres_adapter: PostgresAdapter) -> None:
    """Verify get_stats aggregates success counters and latency telemetry accurately."""
    postgres_adapter.record_request("k1", "success", latency=0.1)
    stats = postgres_adapter.get_stats("k1")
    assert stats is not None
    assert stats.key == "k1"
    assert stats.total_requests == 1
    assert stats.total_success == 1
    assert stats.latency_sum == pytest.approx(0.1)
    assert stats.latency_min == pytest.approx(0.1)
    assert stats.latency_max == pytest.approx(0.1)
    assert stats.latency_count == 1
    assert stats.last_used_at > 0


def test_get_all_stats(postgres_adapter: PostgresAdapter) -> None:
    """Verify get_all_stats returns cumulative statistics for all active keys."""
    postgres_adapter.record_request("k1", "success")
    postgres_adapter.record_request("k2", "error")

    all_stats = {s.key: s for s in postgres_adapter.get_all_stats()}
    assert "k1" in all_stats and "k2" in all_stats
    assert all_stats["k1"].total_success == 1
    assert all_stats["k2"].total_error == 1


def test_reset_stats_clears(postgres_adapter: PostgresAdapter) -> None:
    """Verify reset_stats clears a single key or all keys depending on parameter."""
    postgres_adapter.record_request("k1", "success")
    postgres_adapter.record_request("k2", "error")

    postgres_adapter.reset_stats("k1")
    assert postgres_adapter.get_stats("k1").total_requests == 0
    assert postgres_adapter.get_stats("k2").total_requests == 1

    postgres_adapter.reset_stats()
    assert postgres_adapter.get_stats("k2").total_requests == 0
    assert postgres_adapter.get_all_stats() == []


def test_close_is_idempotent(sync_test_dsn: str, ensure_test_schema: None) -> None:
    """Verify calling close multiple times does not raise any error."""
    adapter = PostgresAdapter(sync_test_dsn)
    adapter.close()
    adapter.close()
    adapter.close()


def test_close_then_operation_auto_reconnects(
    sync_test_dsn: str,
    ensure_test_schema: None,
) -> None:
    """Verify closed adapter automatically reconnects on the next operation."""
    adapter = PostgresAdapter(sync_test_dsn)
    adapter.close()
    adapter.mark_key_suspended("k1", 1000.0)
    assert adapter.get_suspended_keys() == {"k1"}
    adapter.close()


def test_convert_dsn_from_asyncpg_format() -> None:
    """Verify GeminiProvider._convert_dsn converts asyncpg driver prefixes to sync psycopg."""
    assert (
        GeminiProvider._convert_dsn("postgresql+asyncpg://user:pass@127.0.0.1:5432/db")
        == "postgresql://user:pass@127.0.0.1:5432/db"
    )
    assert (
        GeminiProvider._convert_dsn("postgresql://user:pass@127.0.0.1:5432/db")
        == "postgresql://user:pass@127.0.0.1:5432/db"
    )
    assert (
        GeminiProvider._convert_dsn("postgres+asyncpg://user:pass@127.0.0.1:5432/db")
        == "postgresql://user:pass@127.0.0.1:5432/db"
    )
