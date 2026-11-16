"""Phase Y-9: queue freshness — age filter, expiry and newest-first selection."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.dialects import postgresql

from app.config import settings
from app.processors import pipeline
from app.processors.pipeline import (
    article_age_hours,
    build_processing_query,
    collect_new_articles,
    expire_stale_collected,
    freshness_cutoff,
)

NOW = datetime(2026, 11, 16, 8, 0, tzinfo=UTC)


class _Result:
    def __init__(self, items: list[object]):
        self._items = items

    def all(self) -> list[object]:
        return self._items


class _Session:
    """Minimal async session: enough for the queue helpers, no database."""

    def __init__(self, *, scalars_items=None, scalar_value=None, rowcount=0):
        self._scalars = scalars_items if scalars_items is not None else []
        self._scalar = scalar_value
        self.rowcount = rowcount
        self.executed: list[object] = []
        self.added: list[object] = []
        self.commits = 0

    async def scalars(self, stmt: object) -> _Result:
        return _Result(self._scalars)

    async def scalar(self, stmt: object) -> object:
        return self._scalar

    async def execute(self, stmt: object) -> SimpleNamespace:
        self.executed.append(stmt)
        return SimpleNamespace(rowcount=self.rowcount)

    def add(self, obj: object) -> None:
        self.added.append(obj)

    async def flush(self) -> None:
        return None

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        return None


class _Nested:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *exc: object) -> bool:
        return False


class _CollectSession(_Session):
    def begin_nested(self) -> _Nested:
        return _Nested()


# --- A1: age definition -------------------------------------------------------


def test_age_uses_publication_time_even_when_collection_is_fresh() -> None:
    """A week-old story re-served by a feed is old, however recently it was fetched."""
    published = NOW - timedelta(days=3)
    assert article_age_hours(published, NOW - timedelta(minutes=5), NOW) == pytest.approx(72.0)


def test_future_publication_time_counts_as_zero() -> None:
    """Broken feed clocks must not produce a negative age."""
    assert article_age_hours(NOW + timedelta(hours=5), NOW, NOW) == 0.0


def test_missing_publication_time_falls_back_to_collection_time() -> None:
    assert article_age_hours(None, NOW - timedelta(hours=10), NOW) == pytest.approx(10.0)


def test_boundary_exactly_at_the_limit_is_still_fresh(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "article_max_age_hours", 24)
    cutoff = freshness_cutoff(NOW)
    assert article_age_hours(cutoff, None, NOW) == pytest.approx(24.0)


# --- A3: selection ------------------------------------------------------------


def test_processing_query_filters_by_age_and_selects_newest_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Y-12: failed/retry articles get priority (CASE status='failed' THEN 0 ELSE 1)

    so that new articles never starve retries, followed by newest-first ordering.
    """
    monkeypatch.setattr(settings, "article_max_age_hours", 24)
    compiled = build_processing_query(10).compile(dialect=postgresql.dialect())
    sql = str(compiled)
    reference = "coalesce(articles.published_at, articles.collected_at)"

    assert f"{reference} >=" in sql
    assert "CASE WHEN (articles.status =" in sql
    assert sql.index("CASE WHEN") < sql.index(f"{reference} DESC")


def test_processing_query_keeps_only_retryable_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    """Retry logic and the attempt cap are unchanged; expired rows never appear."""
    monkeypatch.setattr(settings, "ai_max_attempts", 3)
    compiled = build_processing_query(10).compile(dialect=postgresql.dialect())
    sql = str(compiled)

    assert "attempt_count <" in sql
    assert "collected" in compiled.params.values()
    assert "failed" in compiled.params.values()
    assert "expired" not in compiled.params.values()
    assert 3 in compiled.params.values()


# --- A2: insertion ------------------------------------------------------------


@pytest.mark.asyncio
async def test_inserted_articles_past_the_limit_are_stored_expired(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The stale story is stored, not skipped, so dedup refuses it next run."""
    monkeypatch.setattr(settings, "article_max_age_hours", 24)
    monkeypatch.setattr(pipeline, "is_duplicate_article", AsyncMock(return_value=False))
    source = SimpleNamespace(
        id=1, name="Source A", feed_url="https://feed.example/rss", language="en", is_active=True
    )
    session = _CollectSession(scalars_items=[source])
    now = datetime.now(UTC)
    entries = [
        {
            "title": "old",
            "url": "https://example.com/old",
            "canonical_url": "https://example.com/old",
            "text": "text",
            "published_at": now - timedelta(hours=25),
            "content_hash": "h1",
        },
        {
            "title": "fresh",
            "url": "https://example.com/fresh",
            "canonical_url": "https://example.com/fresh",
            "text": "text",
            "published_at": now - timedelta(hours=23),
            "content_hash": "h2",
        },
        {
            "title": "no-date",
            "url": "https://example.com/nodate",
            "canonical_url": "https://example.com/nodate",
            "text": "text",
            "published_at": None,
            "content_hash": "h3",
        },
    ]
    monkeypatch.setattr(pipeline, "fetch_feed", AsyncMock(return_value=entries))

    inserted = await collect_new_articles(session)

    assert inserted == 3
    assert {article.title: article.status for article in session.added} == {
        "old": "expired",
        "fresh": "collected",
        "no-date": "collected",
    }


# --- A4: run-start expiry -----------------------------------------------------


@pytest.mark.asyncio
async def test_expiry_is_one_update_and_reports_the_oldest_age(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "article_max_age_hours", 24)
    session = _Session(scalar_value=NOW - timedelta(hours=40), rowcount=17)

    expired, oldest = await expire_stale_collected(session, now=NOW)

    assert expired == 17
    assert oldest == pytest.approx(40.0)
    assert len(session.executed) == 1
    assert str(session.executed[0]).startswith("UPDATE articles")


@pytest.mark.asyncio
async def test_dry_run_measures_without_writing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "article_max_age_hours", 24)
    session = _Session(scalar_value=NOW - timedelta(hours=40), rowcount=17)

    expired, oldest = await expire_stale_collected(session, now=NOW, dry_run=True)

    assert expired == 0
    assert oldest == pytest.approx(40.0)
    assert session.executed == []


@pytest.mark.asyncio
async def test_processing_run_expires_the_stale_tail_before_selecting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "article_max_age_hours", 24)
    monkeypatch.setattr(settings, "immediate_publish_dry_run", False)
    session = _Session(
        scalars_items=[], scalar_value=datetime.now(UTC) - timedelta(hours=30), rowcount=5
    )
    stats: dict[str, object] = {}

    count = await pipeline.process_collected_articles(session, limit=10, queue_stats=stats)

    assert count == 0
    assert stats["expired_now"] == 5
    assert stats["oldest_age_h"] == pytest.approx(30.0, abs=0.1)


@pytest.mark.asyncio
async def test_dry_run_processing_writes_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "immediate_publish_dry_run", True)
    session = _Session(
        scalars_items=[], scalar_value=datetime.now(UTC) - timedelta(hours=30), rowcount=5
    )
    stats: dict[str, object] = {}

    await pipeline.process_collected_articles(session, limit=10, queue_stats=stats)

    assert session.executed == []
    assert stats["expired_now"] == 0


# --- Y-12: expire full table, oldest_age correctness, retry starvation --------


@pytest.mark.asyncio
async def test_oldest_age_reflects_min_not_max(monkeypatch: pytest.MonkeyPatch) -> None:
    """Y-12 Bug 1: oldest_age_h must use MIN (oldest row), not MAX (newest row).

    The session mock returns scalar_value = NOW - 36h, which simulates what
    ``SELECT MIN(coalesce(published_at, collected_at)) WHERE status='collected'``
    returns when the oldest article is 36 hours old and many fresh ones exist.
    Before the fix, func.max returned the timestamp of the newest article (age ≈ 0),
    causing the log to show oldest_age_h=0.1 while 36-hour-old rows sat in DB.
    """
    monkeypatch.setattr(settings, "article_max_age_hours", 24)
    stale_timestamp = NOW - timedelta(hours=36)
    session = _Session(scalar_value=stale_timestamp, rowcount=0)

    _expired, oldest = await expire_stale_collected(session, now=NOW)

    assert oldest is not None
    assert oldest == pytest.approx(36.0), (
        f"oldest_age_h={oldest} but expected ≈36.0 — "
        "func.max (newest-first) was likely used instead of func.min"
    )


@pytest.mark.asyncio
async def test_expire_stale_collected_100_articles_40_hours_old(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Y-12 Bug 2: 100 articles with collected_at 40h ago — system must expire all 100.

    Verifies that expire_stale_collected targets all 100 rows across the table
    in a single statement and returns expired_count=100.
    """
    monkeypatch.setattr(settings, "article_max_age_hours", 24)
    stale_timestamp = NOW - timedelta(hours=40)
    session = _Session(scalar_value=stale_timestamp, rowcount=100)

    expired, oldest = await expire_stale_collected(session, now=NOW)

    assert expired == 100
    assert oldest == pytest.approx(40.0)
    assert len(session.executed) == 1
    stmt_str = str(session.executed[0])
    assert stmt_str.startswith("UPDATE articles")
    # Must apply to the full table, not a restricted subset
    assert "IN (" not in stmt_str


@pytest.mark.asyncio
async def test_dry_run_reports_real_oldest_age_without_writing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Y-12: dry_run=True must still compute and return the real oldest_age_h."""
    monkeypatch.setattr(settings, "article_max_age_hours", 24)
    stale_timestamp = NOW - timedelta(hours=36)
    session = _Session(scalar_value=stale_timestamp, rowcount=50)

    expired, oldest = await expire_stale_collected(session, now=NOW, dry_run=True)

    assert expired == 0
    assert session.executed == []
    assert oldest == pytest.approx(36.0)


def test_retry_priority_guarantees_slots_when_new_exceeds_batch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Y-12 Bug 3: failed articles get a slot even when new articles exceed batch capacity.

    Verifies the query ordering gives priority to status='failed' articles so that
    a large volume of fresh articles cannot starve retryable failures.
    """
    monkeypatch.setattr(settings, "article_max_age_hours", 24)
    monkeypatch.setattr(settings, "ai_max_attempts", 3)
    compiled = build_processing_query(10).compile(dialect=postgresql.dialect())
    sql = str(compiled)
    reference = "coalesce(articles.published_at, articles.collected_at)"

    # Failed articles receive priority 0 via CASE WHEN, so they sort ahead of collected (priority 1)
    assert "CASE WHEN (articles.status =" in sql
    assert sql.index("CASE WHEN") < sql.index(f"{reference} DESC")
