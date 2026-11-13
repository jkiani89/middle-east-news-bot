"""Integration tests for app.processors.pipeline.collect_and_process (Phase C).

Tests C-01 to C-09 verify happy path, resilience, deduplication, batch limits, and fatal error propagation.
Test C-10 proves the rollback bug (ISS-031) where JobLog is lost on article IntegrityError.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from app.ai.provider import MockAIProvider
from app.config import settings
from app.database.models import Article, JobLog, Source, Summary
from app.processors.pipeline import collect_and_process


class _Savepoint:
    """Simulates session.begin_nested() savepoint semantics."""

    def __init__(self, session: RollbackAwareMockSession):
        self.session = session
        self._pending_snap: list[Any] | None = None
        self._flushed_snap: list[Any] | None = None

    async def __aenter__(self) -> _Savepoint:
        self._pending_snap = list(self.session.pending_adds)
        self._flushed_snap = list(self.session.flushed)
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, tb: Any) -> bool:
        if exc_type is not None:
            if self._pending_snap is not None:
                self.session.pending_adds = self._pending_snap
            if self._flushed_snap is not None:
                self.session.flushed = self._flushed_snap
        return False


class RollbackAwareMockSession:
    """AsyncSession simulation with accurate rollback semantics.

    - add() -> pending_adds
    - flush() -> pending_adds to flushed; IntegrityError if canonical_url already exists in objects
    - commit() -> flushed to objects
    - rollback() -> pending_adds and flushed are cleared, objects remain intact
    """

    def __init__(self, objects: list[Any] | None = None):
        self.objects: list[Any] = list(objects) if objects else []
        self.pending_adds: list[Any] = []
        self.flushed: list[Any] = []
        self.commit_count = 0
        self.rollback_count = 0
        self.flush_count = 0
        self._next_id = 100

    def begin_nested(self) -> _Savepoint:
        return _Savepoint(self)

    def add(self, obj: Any) -> None:
        self.pending_adds.append(obj)

    async def flush(self) -> None:
        existing_urls = {a.canonical_url for a in self.objects if isinstance(a, Article)}
        for obj in self.pending_adds:
            if isinstance(obj, Article) and obj.canonical_url in existing_urls:
                from sqlalchemy.exc import IntegrityError
                raise IntegrityError("UNIQUE constraint failed: articles.canonical_url", None, None)
            if getattr(obj, "id", None) is None:
                obj.id = self._next_id
                self._next_id += 1

        self.flushed.extend(self.pending_adds)
        self.pending_adds = []
        self.flush_count += 1

    async def commit(self) -> None:
        await self.flush()
        self.objects.extend(self.flushed)
        self.flushed = []
        self.commit_count += 1

    async def rollback(self) -> None:
        self.pending_adds = []
        self.flushed = []  # Critical: flushed items are lost upon rollback
        self.rollback_count += 1

    async def scalars(self, stmt: Any) -> _ScalarsResult:
        all_available = self.objects + self.flushed
        return _ScalarsResult(all_available, stmt)

    async def scalar(self, stmt: Any) -> Any:
        res = await self.scalars(stmt)
        items = res.all()
        return items[0] if items else None

    async def execute(self, stmt: Any) -> _ExecuteResult:
        all_available = self.objects + self.flushed
        return _ExecuteResult(all_available, stmt)


class _ScalarsResult:
    def __init__(self, objects: list[Any], stmt: Any = None):
        self._all_objects = objects
        self.stmt = stmt

    def all(self) -> list[Any]:
        if self.stmt is None:
            return self._all_objects

        entity = None
        if hasattr(self.stmt, "column_descriptions") and self.stmt.column_descriptions:
            entity = self.stmt.column_descriptions[0].get("entity")

        where_str = str(getattr(self.stmt, "whereclause", ""))

        if entity is Source:
            sources = [o for o in self._all_objects if isinstance(o, Source)]
            if "is_active" in where_str:
                sources = [s for s in sources if s.is_active is True]
            if "feed_url" in where_str:
                sources = [s for s in sources if s.feed_url is not None]
            return sources

        if entity is Article:
            # Check if querying Article.id for is_duplicate_article
            if hasattr(self.stmt, "column_descriptions") and self.stmt.column_descriptions[0].get("name") == "id":
                params: dict[str, Any] = {}
                try:
                    params = self.stmt.compile().params
                except Exception:
                    pass
                target_url = params.get("canonical_url_1")
                target_hash = params.get("content_hash_1")
                for a in self._all_objects:
                    if isinstance(a, Article):
                        if (target_url and a.canonical_url == target_url) or (target_hash and a.content_hash == target_hash):
                            return [a.id]
                return []

            articles = [o for o in self._all_objects if isinstance(o, Article)]
            if "status" in where_str:
                params = {}
                try:
                    params = self.stmt.compile().params
                except Exception:
                    pass
                target_status = params.get("status_1", "collected")
                articles = [a for a in articles if getattr(a, "status", None) == target_status]
            return articles

        if getattr(entity, "__name__", "") == "Event":
            events = [o for o in self._all_objects if type(o).__name__ == "Event"]
            if "active" in where_str:
                events = [e for e in events if getattr(e, "status", None) == "active"]
            return events

        if entity is Summary:
            return [o for o in self._all_objects if isinstance(o, Summary)]

        if entity is not None:
            return [o for o in self._all_objects if isinstance(o, entity)]
        return self._all_objects

    def first(self) -> Any:
        items = self.all()
        return items[0] if items else None


class _ExecuteResult:
    def __init__(self, objects: list[Any], stmt: Any = None):
        self.objects = objects
        self.stmt = stmt

    def all(self) -> list[tuple[int, str]]:
        return [(s.id, s.name) for s in self.objects if isinstance(s, Source)]


class MockSessionContextManager:
    def __init__(self, session: RollbackAwareMockSession):
        self.session = session

    async def __aenter__(self) -> RollbackAwareMockSession:
        return self.session

    async def __aexit__(self, *a: Any) -> bool:
        return False


VALID_CANNED_PAYLOAD = {
    "is_relevant": True,
    "is_injection_attempt": False,
    "categories": ["POL", "DIP"],
    "event_title": "Regional Diplomatic Convening",
    "headline_fa": "نشست دیپلماتیک منطقه‌ای در تهران",
    "summary_fa": "به گزارش منابع رسمی، گفتگوهای دیپلماتیک جهت حل بحران‌های منطقه‌ای برگزار شد.",
    "countries": ["ایران"],
    "actors": ["وزارت امور خارجه"],
    "location": "Tehran",
    "importance": 3,
    "verification_status": "confirmed",
}


@pytest.mark.asyncio
async def test_happy_path_two_sources_four_articles():
    """C-01: Happy path with two active sources and four valid articles."""
    source1 = Source(id=1, name="Source A", base_url="https://a.com", feed_url="https://a.com/rss", is_active=True, language="en")
    source2 = Source(id=2, name="Source B", base_url="https://b.com", feed_url="https://b.com/rss", is_active=True, language="en")
    session = RollbackAwareMockSession(objects=[source1, source2])

    feed_map = {
        "https://a.com/rss": [
            {"title": "News A1", "url": "https://a.com/1", "canonical_url": "https://a.com/1", "text": "Body A1", "published_at": None, "content_hash": "HASH_A1", "text_source": "rss_summary"},
            {"title": "News A2", "url": "https://a.com/2", "canonical_url": "https://a.com/2", "text": "Body A2", "published_at": None, "content_hash": "HASH_A2", "text_source": "rss_summary"},
        ],
        "https://b.com/rss": [
            {"title": "News B1", "url": "https://b.com/1", "canonical_url": "https://b.com/1", "text": "Body B1", "published_at": None, "content_hash": "HASH_B1", "text_source": "rss_summary"},
            {"title": "News B2", "url": "https://b.com/2", "canonical_url": "https://b.com/2", "text": "Body B2", "published_at": None, "content_hash": "HASH_B2", "text_source": "rss_summary"},
        ],
    }

    async def fake_feed(url: str, **kwargs: Any) -> list[dict[str, Any]]:
        return feed_map.get(url, [])

    provider = MockAIProvider(canned_response=VALID_CANNED_PAYLOAD)

    with patch("app.processors.pipeline.SessionLocal", return_value=MockSessionContextManager(session)), \
         patch("app.processors.pipeline.fetch_feed", new=AsyncMock(side_effect=fake_feed)):
        count = await collect_and_process(provider=provider)

    assert count == 4
    articles = [o for o in session.objects if isinstance(o, Article)]
    assert len(articles) == 4
    summaries = [o for o in session.objects if isinstance(o, Summary)]
    assert len(summaries) >= 1
    joblogs = [o for o in session.objects if isinstance(o, JobLog)]
    assert len(joblogs) == 1
    assert joblogs[0].status == "success"
    assert joblogs[0].processed_count == 4


@pytest.mark.asyncio
async def test_one_source_fails_others_succeed():
    """C-02: One source raises error during fetch_feed, other source succeeds."""
    source_fail = Source(id=1, name="Faulty Source", base_url="https://fail.com", feed_url="https://fail.com/rss", is_active=True, language="en")
    source_ok = Source(id=2, name="Working Source", base_url="https://ok.com", feed_url="https://ok.com/rss", is_active=True, language="en")
    session = RollbackAwareMockSession(objects=[source_fail, source_ok])

    async def fake_feed(url: str, **kwargs: Any) -> list[dict[str, Any]]:
        if "fail.com" in url:
            raise ConnectionError("Network timeout on faulty feed")
        return [
            {"title": "News OK1", "url": "https://ok.com/1", "canonical_url": "https://ok.com/1", "text": "Body OK1", "published_at": None, "content_hash": "HASH_OK1", "text_source": "rss_summary"},
            {"title": "News OK2", "url": "https://ok.com/2", "canonical_url": "https://ok.com/2", "text": "Body OK2", "published_at": None, "content_hash": "HASH_OK2", "text_source": "rss_summary"},
        ]

    provider = MockAIProvider(canned_response=VALID_CANNED_PAYLOAD)

    with patch("app.processors.pipeline.SessionLocal", return_value=MockSessionContextManager(session)), \
         patch("app.processors.pipeline.fetch_feed", new=AsyncMock(side_effect=fake_feed)):
        count = await collect_and_process(provider=provider)

    assert count == 2
    articles = [o for o in session.objects if isinstance(o, Article)]
    assert len(articles) == 2
    joblogs = [o for o in session.objects if isinstance(o, JobLog)]
    assert len(joblogs) == 1
    assert joblogs[0].status == "success"
    assert joblogs[0].processed_count == 2


@pytest.mark.asyncio
async def test_all_sources_fail_logs_success_currently():
    """C-03: When all sources fail, current code logs success with count=0 (documented behavior)."""
    source1 = Source(id=1, name="Fail 1", base_url="https://f1.com", feed_url="https://f1.com/rss", is_active=True, language="en")
    source2 = Source(id=2, name="Fail 2", base_url="https://f2.com", feed_url="https://f2.com/rss", is_active=True, language="en")
    session = RollbackAwareMockSession(objects=[source1, source2])

    async def fake_feed(url: str, **kwargs: Any) -> list[dict[str, Any]]:
        raise TimeoutError("All feeds down")

    provider = MockAIProvider(canned_response=VALID_CANNED_PAYLOAD)

    with patch("app.processors.pipeline.SessionLocal", return_value=MockSessionContextManager(session)), \
         patch("app.processors.pipeline.fetch_feed", new=AsyncMock(side_effect=fake_feed)):
        count = await collect_and_process(provider=provider)

    assert count == 0
    articles = [o for o in session.objects if isinstance(o, Article)]
    assert len(articles) == 0
    joblogs = [o for o in session.objects if isinstance(o, JobLog)]
    assert len(joblogs) == 1
    assert joblogs[0].status == "success"
    assert joblogs[0].processed_count == 0


@pytest.mark.asyncio
async def test_duplicate_canonical_url_skipped():
    """C-04: Existing article with canonical_url=X is skipped; new article Y is collected."""
    source = Source(id=1, name="Source A", base_url="https://a.com", feed_url="https://a.com/rss", is_active=True, language="en")
    existing_article = Article(
        id=1,
        source_id=1,
        title="Existing News",
        original_url="https://a.com/existing",
        canonical_url="https://a.com/existing",
        original_text="Old Text",
        content_hash="OLD_HASH_111",
        status="summarized",
    )
    session = RollbackAwareMockSession(objects=[source, existing_article])

    async def fake_feed(url: str, **kwargs: Any) -> list[dict[str, Any]]:
        return [
            {"title": "Existing News", "url": "https://a.com/existing?utm=1", "canonical_url": "https://a.com/existing", "text": "Old Text", "published_at": None, "content_hash": "OLD_HASH_111", "text_source": "rss_summary"},
            {"title": "Brand New News", "url": "https://a.com/new", "canonical_url": "https://a.com/new", "text": "New Text", "published_at": None, "content_hash": "NEW_HASH_222", "text_source": "rss_summary"},
        ]

    provider = MockAIProvider(canned_response=VALID_CANNED_PAYLOAD)

    with patch("app.processors.pipeline.SessionLocal", return_value=MockSessionContextManager(session)), \
         patch("app.processors.pipeline.fetch_feed", new=AsyncMock(side_effect=fake_feed)):
        count = await collect_and_process(provider=provider)

    assert count == 1
    articles = [o for o in session.objects if isinstance(o, Article)]
    assert len(articles) == 2  # 1 existing + 1 newly collected
    newly_added = [a for a in articles if a.canonical_url == "https://a.com/new"]
    assert len(newly_added) == 1
    assert newly_added[0].status == "summarized"


@pytest.mark.asyncio
async def test_duplicate_content_hash_different_url_skipped():
    """C-05: Existing article with content_hash=H1 is skipped even if URL is different."""
    source = Source(id=1, name="Source A", base_url="https://a.com", feed_url="https://a.com/rss", is_active=True, language="en")
    existing_article = Article(
        id=1,
        source_id=1,
        title="Syndicated News",
        original_url="https://reprint.com/story1",
        canonical_url="https://reprint.com/story1",
        original_text="Same Content Text",
        content_hash="SHARED_HASH_999",
        status="summarized",
    )
    session = RollbackAwareMockSession(objects=[source, existing_article])

    async def fake_feed(url: str, **kwargs: Any) -> list[dict[str, Any]]:
        return [
            {"title": "Syndicated News Alternate URL", "url": "https://a.com/another-url", "canonical_url": "https://a.com/another-url", "text": "Same Content Text", "published_at": None, "content_hash": "SHARED_HASH_999", "text_source": "rss_summary"},
            {"title": "Unique News", "url": "https://a.com/unique", "canonical_url": "https://a.com/unique", "text": "Different Text", "published_at": None, "content_hash": "UNIQUE_HASH_888", "text_source": "rss_summary"},
        ]

    provider = MockAIProvider(canned_response=VALID_CANNED_PAYLOAD)

    with patch("app.processors.pipeline.SessionLocal", return_value=MockSessionContextManager(session)), \
         patch("app.processors.pipeline.fetch_feed", new=AsyncMock(side_effect=fake_feed)):
        count = await collect_and_process(provider=provider)

    assert count == 1
    articles = [o for o in session.objects if isinstance(o, Article)]
    assert len(articles) == 2  # 1 existing + 1 newly collected
    newly_added = [a for a in articles if a.canonical_url == "https://a.com/unique"]
    assert len(newly_added) == 1


@pytest.mark.asyncio
async def test_inactive_source_not_fetched():
    """C-06: Inactive sources (is_active=False) must not be queried or fetched."""
    source_active = Source(id=1, name="Active Source", base_url="https://act.com", feed_url="https://act.com/rss", is_active=True, language="en")
    source_inactive = Source(id=2, name="Inactive Source", base_url="https://inact.com", feed_url="https://inact.com/rss", is_active=False, language="en")
    session = RollbackAwareMockSession(objects=[source_active, source_inactive])

    mock_fetch = AsyncMock(return_value=[
        {"title": "Active News", "url": "https://act.com/1", "canonical_url": "https://act.com/1", "text": "Text", "published_at": None, "content_hash": "HASH_ACT_1", "text_source": "rss_summary"},
    ])
    provider = MockAIProvider(canned_response=VALID_CANNED_PAYLOAD)

    with patch("app.processors.pipeline.SessionLocal", return_value=MockSessionContextManager(session)), \
         patch("app.processors.pipeline.fetch_feed", new=mock_fetch):
        count = await collect_and_process(provider=provider)

    assert count == 1
    assert mock_fetch.call_count == 1
    assert mock_fetch.call_args[0][0] == "https://act.com/rss"


@pytest.mark.asyncio
async def test_source_with_null_feed_url_skipped():
    """C-07: Sources with feed_url=None must be filtered out without calling fetch_feed."""
    source_valid = Source(id=1, name="Valid Feed", base_url="https://val.com", feed_url="https://val.com/rss", is_active=True, language="en")
    source_null_feed = Source(id=2, name="Null Feed", base_url="https://null.com", feed_url=None, is_active=True, language="en")
    session = RollbackAwareMockSession(objects=[source_valid, source_null_feed])

    mock_fetch = AsyncMock(return_value=[
        {"title": "Valid News", "url": "https://val.com/1", "canonical_url": "https://val.com/1", "text": "Text", "published_at": None, "content_hash": "HASH_VAL_1", "text_source": "rss_summary"},
    ])
    provider = MockAIProvider(canned_response=VALID_CANNED_PAYLOAD)

    with patch("app.processors.pipeline.SessionLocal", return_value=MockSessionContextManager(session)), \
         patch("app.processors.pipeline.fetch_feed", new=mock_fetch):
        count = await collect_and_process(provider=provider)

    assert count == 1
    assert mock_fetch.call_count == 1
    assert mock_fetch.call_args[0][0] == "https://val.com/rss"


@pytest.mark.asyncio
async def test_max_articles_per_batch_limit_respected(monkeypatch):
    """C-08: Entries exceeding max_articles_per_batch must not be collected or processed."""
    source = Source(id=1, name="High Volume Source", base_url="https://vol.com", feed_url="https://vol.com/rss", is_active=True, language="en")
    session = RollbackAwareMockSession(objects=[source])

    # 10 entries returned by feed
    entries = [
        {"title": f"News {i}", "url": f"https://vol.com/{i}", "canonical_url": f"https://vol.com/{i}", "text": f"Body {i}", "published_at": None, "content_hash": f"HASH_{i}", "text_source": "rss_summary"}
        for i in range(10)
    ]
    mock_fetch = AsyncMock(return_value=entries)
    monkeypatch.setattr(settings, "max_articles_per_batch", 3)

    provider = MockAIProvider(canned_response=VALID_CANNED_PAYLOAD)

    with patch("app.processors.pipeline.SessionLocal", return_value=MockSessionContextManager(session)), \
         patch("app.processors.pipeline.fetch_feed", new=mock_fetch):
        count = await collect_and_process(provider=provider)

    assert count == 3
    articles = [o for o in session.objects if isinstance(o, Article)]
    assert len(articles) == 3


@pytest.mark.asyncio
async def test_fatal_error_in_process_raises_and_logs():
    """C-09: Fatal error during collection propagates and logs failure to JobLog."""
    session = RollbackAwareMockSession()
    # Mock scalars raising a fatal database connection error
    session.scalars = AsyncMock(side_effect=RuntimeError("Fatal database connection crash"))

    provider = MockAIProvider(canned_response=VALID_CANNED_PAYLOAD)

    with patch("app.processors.pipeline.SessionLocal", return_value=MockSessionContextManager(session)), \
         patch("app.processors.pipeline.fetch_feed", new=AsyncMock()):
        with pytest.raises(RuntimeError, match="Fatal database connection crash"):
            await collect_and_process(provider=provider)

    joblogs = [o for o in session.objects if isinstance(o, JobLog)]
    assert len(joblogs) == 1
    assert joblogs[0].status == "failed"
    assert "Fatal database connection crash" in (joblogs[0].error_message or "")


@pytest.mark.asyncio
async def test_rollback_in_integrity_error_loses_joblog_BUG_PROOF():
    """Regression test for ISS-031.

    HISTORICAL: Added in Phase C to prove the JobLog-loss bug. Failed
    on pre-fix code (rollback cleared flushed JobLog).

    FIXED in Phase D via savepoint (session.begin_nested). Test now
    passes because savepoint isolates the failed article without
    destroying the outer transaction.

    If this test ever fails again, the savepoint fix was broken.
    """
    from app.ai.provider import MockAIProvider
    from app.database.models import Article, JobLog, Source
    from app.processors.pipeline import collect_and_process

    session = RollbackAwareMockSession()
    source = Source(id=1, name="Test", base_url="https://t.com",
                    feed_url="https://t.com/rss", is_active=True, language="en")
    existing = Article(id=1, source_id=1, title="Old",
                       original_url="https://t.com/X",
                       canonical_url="https://t.com/X", original_text="old",
                       content_hash="OLD_HASH", status="summarized")
    session.objects.extend([source, existing])

    valid = {"is_relevant": True, "is_injection_attempt": False,
             "categories": ["POL"], "event_title": "Test",
             "headline_fa": "تیتر", "summary_fa": "خلاصه",
             "countries": ["Iran"], "actors": [], "location": None,
             "importance": 3, "verification_status": "unconfirmed"}
    provider = MockAIProvider(canned_response=valid)

    def fake_feed(url, **kwargs):
        return [
            {"url": "https://t.com/X", "canonical_url": "https://t.com/X",
             "title": "Dup", "text": "dup",
             "content_hash": "NEW_HASH_X", "published_at": None,
             "text_source": "rss_summary"},
            {"url": "https://t.com/Y", "canonical_url": "https://t.com/Y",
             "title": "New", "text": "new",
             "content_hash": "NEW_HASH_Y", "published_at": None,
             "text_source": "rss_summary"},
        ]

    async def fake_is_dup(session, canonical_url, content_hash): return False

    with patch("app.processors.pipeline.SessionLocal",
               return_value=MockSessionContextManager(session)), \
         patch("app.processors.pipeline.fetch_feed",
               new=AsyncMock(side_effect=fake_feed)), \
         patch("app.processors.pipeline.is_duplicate_article", new=fake_is_dup):
        await collect_and_process(provider=provider)

    joblog_count = len([o for o in session.objects if isinstance(o, JobLog)])
    assert joblog_count >= 1, (
        f"BUG CONFIRMED (ISS-031): JobLog lost via rollback. "
        f"Expected >=1, got {joblog_count}."
    )
