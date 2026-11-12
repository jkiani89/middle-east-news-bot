"""Tests for immediate per-article Telegram publishing."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from app.config import settings
from app.database.models import Article
from app.processors import pipeline
from app.telegram.publisher import ImmediateAnalysis, format_immediate_analysis, publish_immediate_analysis

ANALYSIS = ImmediateAnalysis(
    event_title="Regional meeting",
    headline_fa="نشست منطقه‌ای در تهران برگزار شد",
    summary_fa="به گزارش منابع رسمی، نشست منطقه‌ای با حضور وزیران خارجه برگزار شد.",
    source_name="UN News",
    source_url="https://news.un.org/example?a=1&b=2",
    verification_status="confirmed",
    importance=4,
    category="DIP",
    countries=["Iran"],
    actors=["وزارت امور خارجه"],
)


class _FakeMessage:
    message_id = 4242


class _FakeBot:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self.send_message = AsyncMock(side_effect=self._send)

    async def _send(self, **kwargs: Any) -> _FakeMessage:
        self.sent.append(kwargs)
        return _FakeMessage()


class _Result:
    def __init__(self, items: list[Any]):
        self._items = items

    def all(self) -> list[Any]:
        return self._items


class FakeSession:
    def __init__(self, articles: list[Article]):
        self.articles = articles
        self.commit_count = 0
        self.added: list[Any] = []

    def add(self, obj: Any) -> None:
        self.added.append(obj)

    async def flush(self) -> None:
        return None

    async def scalars(self, stmt: Any) -> _Result:
        return _Result(self.articles)

    async def execute(self, stmt: Any) -> _Result:
        return _Result([(1, "UN News")])

    async def commit(self) -> None:
        self.commit_count += 1

    async def rollback(self) -> None:
        return None


def _article(article_id: int) -> Article:
    return Article(
        id=article_id,
        source_id=1,
        title=f"Article {article_id}",
        original_url=f"https://example.com/{article_id}",
        canonical_url=f"https://example.com/{article_id}",
        original_text="body",
        content_hash=f"HASH_{article_id}",
        status="collected",
        attempt_count=0,
    )


def test_formatted_message_contains_headline_summary_and_escaped_source():
    text = format_immediate_analysis(ANALYSIS)

    assert ANALYSIS.headline_fa in text
    assert ANALYSIS.summary_fa in text
    assert "https://news.un.org/example?a=1&amp;b=2" in text
    assert "تحلیل فوری" in text
    assert "⭐" * 4 in text


@pytest.mark.asyncio
async def test_dry_run_sends_nothing_and_prints_block(capsys):
    bot = _FakeBot()

    sent = await publish_immediate_analysis(ANALYSIS, bot=bot, dry_run=True)

    bot.send_message.assert_not_called()
    out = capsys.readouterr().out
    assert "[DRY RUN]" in out
    assert sent and sent[0]["type"] == "immediate"


@pytest.mark.asyncio
async def test_real_publish_sends_message_with_delay(monkeypatch):
    monkeypatch.setattr(settings, "immediate_publish_delay_seconds", 0.0)
    monkeypatch.setattr(settings, "telegram_channel_id", "-100123")
    bot = _FakeBot()

    sent = await publish_immediate_analysis(ANALYSIS, bot=bot, dry_run=False)

    assert bot.send_message.call_count == 1
    kwargs = bot.send_message.call_args.kwargs
    assert kwargs["chat_id"] == "-100123"
    assert ANALYSIS.headline_fa in kwargs["text"]
    assert sent[0]["message_id"] == 4242


@pytest.mark.asyncio
async def test_helper_skips_when_disabled(monkeypatch):
    monkeypatch.setattr(settings, "app_env", "production")
    monkeypatch.setattr(settings, "immediate_publish_enabled", False)
    publisher = AsyncMock()
    monkeypatch.setattr(pipeline, "publish_immediate_analysis", publisher)

    assert await pipeline._publish_immediate_safely(ANALYSIS) is False
    publisher.assert_not_called()


@pytest.mark.asyncio
async def test_helper_skips_in_test_env(monkeypatch):
    monkeypatch.setattr(settings, "app_env", "test")
    monkeypatch.setattr(settings, "immediate_publish_enabled", True)
    publisher = AsyncMock()
    monkeypatch.setattr(pipeline, "publish_immediate_analysis", publisher)

    assert await pipeline._publish_immediate_safely(ANALYSIS) is False
    publisher.assert_not_called()


@pytest.mark.asyncio
async def test_telegram_failure_never_breaks_the_batch(monkeypatch):
    monkeypatch.setattr(settings, "app_env", "production")
    monkeypatch.setattr(settings, "immediate_publish_enabled", True)
    monkeypatch.setattr(settings, "immediate_publish_delay_seconds", 0.0)
    monkeypatch.setattr(
        pipeline, "publish_immediate_analysis", AsyncMock(side_effect=RuntimeError("telegram down"))
    )

    assert await pipeline._publish_immediate_safely(ANALYSIS) is False


@pytest.mark.asyncio
async def test_publish_cap_limits_messages_per_run(monkeypatch):
    from app.ai.provider import MockAIProvider

    monkeypatch.setattr(settings, "app_env", "production")
    monkeypatch.setattr(settings, "immediate_publish_enabled", True)
    monkeypatch.setattr(settings, "immediate_publish_only_new_events", True)
    monkeypatch.setattr(settings, "immediate_publish_max_per_run", 2)
    monkeypatch.setattr(settings, "immediate_publish_delay_seconds", 0.0)

    class _MatchResult:
        is_new_event = True
        event_id = 1
        match_type = "initial"
        confidence = 0.9

    async def fake_match(self, session, article, analysis_data, source_name):
        return _MatchResult()

    monkeypatch.setattr("app.processors.pipeline.EventMatcher.match_or_create_event", fake_match)
    publisher = AsyncMock(return_value=[{"message_id": 1, "type": "immediate"}])
    monkeypatch.setattr(pipeline, "publish_immediate_analysis", publisher)

    payload = {
        "is_relevant": True,
        "is_injection_attempt": False,
        "categories": ["POL"],
        "event_title": "Event",
        "headline_fa": "تیتر فارسی",
        "summary_fa": "خلاصه فارسی",
        "countries": ["Iran"],
        "actors": [],
        "location": None,
        "importance": 3,
        "verification_status": "unconfirmed",
    }
    articles = [_article(i) for i in range(1, 4)]
    session = FakeSession(articles)

    count = await pipeline.process_collected_articles(
        session, limit=10, provider=MockAIProvider(canned_response=payload)
    )

    assert count == 3
    assert publisher.await_count == 2
    assert all(a.status == "summarized" for a in articles)
