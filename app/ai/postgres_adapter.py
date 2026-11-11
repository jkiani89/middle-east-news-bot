"""PostgreSQL adapter for gemini-api-rotator implementing DBAdapter via psycopg3."""

import logging
import time
from typing import Any, Self

import psycopg
from gemini_rotator.adapters import DBAdapter
from gemini_rotator.models import KeyStats
from psycopg.rows import dict_row

logger = logging.getLogger(__name__)


class PostgresAdapter(DBAdapter):
    """Synchronous DBAdapter backed by PostgreSQL via psycopg3.

    This adapter manages rotator key persistence and stats directly in PostgreSQL,
    avoiding event loop blocks through indexed, fast (<1ms) queries with autocommit.
    """

    def __init__(self, dsn: str) -> None:
        if dsn.startswith("postgresql+asyncpg://"):
            dsn = dsn.replace("postgresql+asyncpg://", "postgresql://", 1)
        self._dsn = dsn
        self._conn: psycopg.Connection[dict[str, Any]] | None = None
        self._connect()

    def _connect(self) -> None:
        if self._conn is None or self._conn.closed:
            self._conn = psycopg.connect(
                self._dsn,
                autocommit=True,
                row_factory=dict_row,
            )

    def _ensure(self) -> psycopg.Connection[dict[str, Any]]:
        if self._conn is None or self._conn.closed:
            self._connect()
        assert self._conn is not None
        return self._conn

    def close(self) -> None:
        """Close the underlying PostgreSQL connection idempotently."""
        if self._conn is not None:
            try:
                if not self._conn.closed:
                    self._conn.close()
            except Exception:
                logger.warning("Error closing PostgreSQL connection", exc_info=True)
            finally:
                self._conn = None

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    # ── Key Suspension ─────────────────────────────────────────────────────────

    def get_suspended_keys(self) -> set[str]:
        """Return the set of currently suspended key strings."""
        conn = self._ensure()
        with conn.cursor() as cur:
            cur.execute("SELECT key FROM gemini_suspended_keys")
            rows = cur.fetchall()
            return {r["key"] for r in rows}

    def mark_key_suspended(self, key: str, suspended_at: float | None = None) -> None:
        """Persist that `key` is suspended."""
        ts = suspended_at if suspended_at is not None else time.time()
        conn = self._ensure()
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO gemini_suspended_keys (key, suspended_at)
                VALUES (%s, %s)
                ON CONFLICT (key) DO UPDATE SET suspended_at = EXCLUDED.suspended_at
                """,
                (key, ts),
            )

    def unmark_key_suspended(self, key: str) -> None:
        """Remove `key` from the suspended set."""
        conn = self._ensure()
        with conn.cursor() as cur:
            cur.execute("DELETE FROM gemini_suspended_keys WHERE key = %s", (key,))

    # ── Requests and Stats ────────────────────────────────────────────────────

    def record_request(
        self,
        key: str,
        event: str,
        latency: float | None = None,
        model: str | None = None,
        *,
        latency_ms: float | None = None,
    ) -> None:
        """Record one completed or failed request and update key stats atomically."""
        now = time.time()
        lat_sec = (
            latency
            if (latency is not None and latency >= 0)
            else (latency_ms / 1000.0 if latency_ms is not None and latency_ms >= 0 else None)
        )
        lat_milli = (
            latency_ms
            if latency_ms is not None
            else (latency * 1000.0 if latency is not None and latency >= 0 else None)
        )

        col_map = {
            "success": "total_success",
            "rate_limit": "total_rate_limit",
            "suspended": "total_suspended",
            "transient": "total_transient",
        }
        event_col = col_map.get(event, "total_error")

        conn = self._ensure()
        with conn.cursor() as cur:
            if lat_sec is not None:
                cur.execute(
                    f"""
                    INSERT INTO gemini_key_stats (
                        key, total_requests, {event_col},
                        latency_sum, latency_min, latency_max, latency_count, last_used_at
                    )
                    VALUES (%s, 1, 1, %s, %s, %s, 1, %s)
                    ON CONFLICT (key) DO UPDATE SET
                        total_requests = gemini_key_stats.total_requests + 1,
                        {event_col} = gemini_key_stats.{event_col} + 1,
                        latency_sum = gemini_key_stats.latency_sum + EXCLUDED.latency_sum,
                        latency_min = LEAST(gemini_key_stats.latency_min, EXCLUDED.latency_min),
                        latency_max = GREATEST(gemini_key_stats.latency_max, EXCLUDED.latency_max),
                        latency_count = gemini_key_stats.latency_count + 1,
                        last_used_at = EXCLUDED.last_used_at
                    """,
                    (key, lat_sec, lat_sec, lat_sec, now),
                )
            else:
                cur.execute(
                    f"""
                    INSERT INTO gemini_key_stats (
                        key, total_requests, {event_col}, last_used_at
                    )
                    VALUES (%s, 1, 1, %s)
                    ON CONFLICT (key) DO UPDATE SET
                        total_requests = gemini_key_stats.total_requests + 1,
                        {event_col} = gemini_key_stats.{event_col} + 1,
                        last_used_at = EXCLUDED.last_used_at
                    """,
                    (key, now),
                )

            cur.execute(
                """
                INSERT INTO gemini_request_log (key, event, latency_ms, model, ts)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (key, event, lat_milli, model, now),
            )

    def get_stats(self, key: str) -> KeyStats:
        """Return cumulative stats for a single key."""
        conn = self._ensure()
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM gemini_key_stats WHERE key = %s", (key,))
            row = cur.fetchone()
            if row is None:
                return KeyStats(key=key)
            return self._row_to_stats(row)

    def get_all_stats(self) -> list[KeyStats]:
        """Return stats for every known key."""
        conn = self._ensure()
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM gemini_key_stats")
            rows = cur.fetchall()
            return [self._row_to_stats(r) for r in rows]

    def reset_stats(self, key: str | None = None) -> None:
        """Reset stats. If `key` is given, reset only that key; otherwise reset all."""
        conn = self._ensure()
        with conn.cursor() as cur:
            if key:
                cur.execute("DELETE FROM gemini_key_stats WHERE key = %s", (key,))
                cur.execute("DELETE FROM gemini_request_log WHERE key = %s", (key,))
            else:
                cur.execute("DELETE FROM gemini_key_stats")
                cur.execute("DELETE FROM gemini_request_log")

    def get_latency_history(
        self,
        key: str | None = None,
        limit: int = 500,
        model: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return recent per-request latency rows from gemini_request_log."""
        clauses = ["event = 'success'", "latency_ms IS NOT NULL"]
        params: list[Any] = []

        if key:
            clauses.append("key = %s")
            params.append(key)
        if model:
            clauses.append("model = %s")
            params.append(model)

        where = " AND ".join(clauses)
        params.append(limit)

        conn = self._ensure()
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT key, event, latency_ms, model, ts
                FROM gemini_request_log
                WHERE {where}
                ORDER BY id DESC
                LIMIT %s
                """,
                tuple(params),
            )
            rows = cur.fetchall()
            return [dict(r) for r in rows]

    @staticmethod
    def _row_to_stats(r: dict[str, Any]) -> KeyStats:
        s = KeyStats(key=r["key"])
        s.total_requests = int(r["total_requests"] or 0)
        s.total_success = int(r["total_success"] or 0)
        s.total_rate_limit = int(r["total_rate_limit"] or 0)
        s.total_suspended = int(r["total_suspended"] or 0)
        s.total_transient = int(r["total_transient"] or 0)
        s.total_error = int(r["total_error"] or 0)
        s.latency_sum = float(r["latency_sum"] or 0.0)
        s.latency_min = float(r["latency_min"]) if r["latency_min"] is not None else float("inf")
        s.latency_max = float(r["latency_max"] or 0.0)
        s.latency_count = int(r["latency_count"] or 0)
        s.last_used_at = float(r["last_used_at"] or 0.0)
        return s
