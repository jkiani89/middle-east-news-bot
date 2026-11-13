"""Tests for Phase D resilience: per-article commits, retries, and the 503 breaker."""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy.dialects import postgresql

from app.ai.provider import AIProviderError, AIServiceUnavailableError, BaseAIProvider, MockAIProvider
from app.config import settings
from app.database.models import Article
from app.processors.pipeline import (
    build_processing_query,
    last_batch_stopped_by_circuit_breaker,
    process_collected_articles,
)

IRRELEVANT_PAYLOAD: dict[str, Any] = {
    "is_relevant": False,
    "is_injection_attempt": False,
    "categories": [],
    "event_title": "Off-topic item",
    "headline_fa": "خبر نامرتبط",
    "summary_fa": "این خبر به خاورمیانه مربوط نیست.",
    "countries": [],
    "actors": [],
    "location": None,
    "importance": 1,
    "verification_status": "unconfirmed",
}

VALID_PAYLOAD: dict[str, Any] = {
    "is_relevant": True,
    "is_injection_attempt": False,
    "categories": ["POL"],
    "event_title": "Regional diplomatic meeting",
    "headline_fa": "نشست دیپلماتیک منطقه‌ای",
    "summary_fa": "به گزارش منابع رسمی، نشست دیپلماتیک برگزار شد.",
    "countries": ["Iran"],
    "actors": [],
    "location": None,
    "importance": 3,
    "verification_status": "unconfirmed",
}


class _Result:
    def __init__(self, items: list[Any]):
        self._items = items

    def all(self) -> list[Any]:
        return self._items


class FakeSession:
    """Minimal async session stand-in for pipeline unit tests."""

    def __init__(self, articles: list[Article], sources: list[tuple[int, str]] | None = None):
        self.articles = articles
        self.sources = sources or [(1, "Source A")]
        self.commit_count = 0
        self.rollback_count = 0
        self.added: list[Any] = []

    def begin_nested(self):
        raise AssertionError("begin_nested is not used while processing articles")

    def add(self, obj: Any) -> None:
        self.added.append(obj)

    async def flush(self) -> None:
        return None

    async def scalars(self, stmt: Any) -> _Result:
        return _Result(self.articles)

    async def scalar(self, stmt: Any) -> Any:
        return 1

    async def execute(self, stmt: Any) -> _Result:
        return _Result(self.sources)

    async def commit(self) -> None:
        self.commit_count += 1

    async def rollback(self) -> None:
        self.rollback_count += 1


class AlwaysUnavailableProvider(BaseAIProvider):
    def __init__(self) -> None:
        self.calls = 0

    @property
    def model_name(self) -> str:
        return "unavailable-model"

    async def generate_json(self, prompt: str, system_instruction: str | None = None, *, purpose: str = "analysis"):
        self.calls += 1
        raise AIServiceUnavailableError("503 UNAVAILABLE: model overloaded")


class AlwaysBrokenProvider(BaseAIProvider):
    def __init__(self) -> None:
        self.calls = 0

    @property
    def model_name(self) -> str:
        return "broken-model"

    async def generate_json(self, prompt: str, system_instruction: str | None = None, *, purpose: str = "analysis"):
        self.calls += 1
        raise AIProviderError("Pydantic validation failed permanently")


class FlakyJsonProvider(BaseAIProvider):
    """Returns truncated JSON first, valid JSON on the schema-repair retry."""

    def __init__(self, payload: dict[str, Any]):
        self.payload = payload
        self.purposes: list[str] = []

    @property
    def model_name(self) -> str:
        return "flaky-json-model"

    async def generate_json(self, prompt: str, system_instruction: str | None = None, *, purpose: str = "analysis"):
        self.purposes.append(purpose)
        if len(self.purposes) == 1:
            raise AIProviderError("Invalid JSON returned by Gemini: Unterminated string starting at line 4")
        return dict(self.payload)


def _article(article_id: int, status: str = "collected", attempt_count: int = 0) -> Article:
    return Article(
        id=article_id,
        source_id=1,
        title=f"Article {article_id}",
        original_url=f"https://example.com/{article_id}",
        canonical_url=f"https://example.com/{article_id}",
        original_text="body",
        content_hash=f"HASH_{article_id}",
        status=status,
        attempt_count=attempt_count,
    )


@pytest.mark.asyncio
async def test_transient_failure_keeps_article_retryable(monkeypatch):
    """A 503 marks the article failed with one attempt recorded, ready for retry."""
    monkeypatch.setattr(settings, "ai_max_consecutive_failures", 5)
    monkeypatch.setattr(settings, "ai_max_attempts", 3)
    article = _article(1)
    session = FakeSession([article])

    count = await process_collected_articles(session, limit=10, provider=AlwaysUnavailableProvider())

    assert count == 0
    assert article.status == "failed"
    assert article.attempt_count == 1
    assert article.last_attempt_at is not None
    assert session.commit_count == 1
    assert last_batch_stopped_by_circuit_breaker() is False


@pytest.mark.asyncio
async def test_circuit_breaker_stops_batch_and_leaves_queue_untouched(monkeypatch):
    """After N consecutive transient errors the batch stops; the rest stays collected."""
    monkeypatch.setattr(settings, "ai_max_consecutive_failures", 3)
    articles = [_article(i) for i in range(1, 9)]
    session = FakeSession(articles)
    provider = AlwaysUnavailableProvider()

    count = await process_collected_articles(session, limit=10, provider=provider)

    assert count == 0
    assert provider.calls == 3
    assert last_batch_stopped_by_circuit_breaker() is True
    attempted = [a for a in articles if a.attempt_count == 1]
    untouched = [a for a in articles if a.attempt_count == 0]
    assert len(attempted) == 3
    assert len(untouched) == 5
    assert all(a.status == "collected" for a in untouched)


@pytest.mark.asyncio
async def test_permanent_failure_is_not_treated_as_transient(monkeypatch):
    """A non-transient error does not trip the breaker."""
    monkeypatch.setattr(settings, "ai_max_consecutive_failures", 2)
    articles = [_article(1), _article(2)]
    session = FakeSession(articles)
    provider = AlwaysBrokenProvider()

    count = await process_collected_articles(session, limit=10, provider=provider)

    assert count == 2
    assert provider.calls == 2
    assert all(a.status == "failed" for a in articles)
    assert last_batch_stopped_by_circuit_breaker() is False


@pytest.mark.asyncio
async def test_each_article_is_committed_individually(monkeypatch):
    """Progress is durable: one commit per processed article."""
    articles = [_article(i) for i in range(1, 4)]
    session = FakeSession(articles)
    provider = MockAIProvider(canned_response=IRRELEVANT_PAYLOAD)

    count = await process_collected_articles(session, limit=10, provider=provider)

    assert count == 3
    assert session.commit_count == 3
    assert all(a.status == "rejected" for a in articles)


def test_processing_query_includes_retryable_failures(monkeypatch):
    """The selection predicate retries failed articles that still have attempts left."""
    monkeypatch.setattr(settings, "ai_max_attempts", 3)
    compiled = build_processing_query(10).compile(dialect=postgresql.dialect())
    sql = str(compiled)

    assert "attempt_count <" in sql
    assert "articles.status =" in sql
    assert "last_attempt_at" in sql
    assert "collected" in compiled.params.values()
    assert "failed" in compiled.params.values()
    assert 3 in compiled.params.values()


@pytest.mark.asyncio
async def test_truncated_json_is_retried_with_schema_repair(monkeypatch):
    """D-4: malformed JSON gets exactly one schema_repair retry instead of failing the article."""
    from app.ai.summarizer import analyze_article

    provider = FlakyJsonProvider(VALID_PAYLOAD)

    analysis = await analyze_article("Title", "Body text", "Source A", provider=provider)

    assert analysis.event_title == "Regional diplomatic meeting"
    assert provider.purposes == ["analysis", "schema_repair"]


@pytest.mark.asyncio
async def test_persistent_malformed_json_surfaces_as_provider_error():
    """If the repair retry is also malformed, the error propagates for the pipeline to record."""
    from app.ai.summarizer import analyze_article

    class BrokenJsonProvider(FlakyJsonProvider):
        async def generate_json(self, prompt, system_instruction=None, *, purpose="analysis"):
            self.purposes.append(purpose)
            raise AIProviderError("Invalid JSON returned by Gemini: truncated")

    provider = BrokenJsonProvider(VALID_PAYLOAD)

    with pytest.raises(AIProviderError):
        await analyze_article("Title", "Body text", "Source A", provider=provider)

    assert provider.purposes == ["analysis", "schema_repair"]
