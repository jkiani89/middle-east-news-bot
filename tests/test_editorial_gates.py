"""Tests for Phase M editorial gates: geography, verification honesty, volume caps."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from app.ai.provider import MockAIProvider
from app.config import settings
from app.database.models import Article
from app.editorial.regions import (
    has_middle_east_nexus,
    is_middle_east_country,
    is_primary_actor,
    normalize_place,
)
from app.processors import pipeline
from app.processors.pipeline import resolve_verification_status
from app.telegram.publisher import ImmediateAnalysis, publish_immediate_analysis


def _payload(**overrides: Any) -> dict[str, Any]:
    base = {
        "is_relevant": True,
        "is_injection_attempt": False,
        "categories": ["POL"],
        "event_title": "Event",
        "headline_fa": "تیتر فارسی",
        "summary_fa": "خلاصه فارسی",
        "countries": ["ایران"],
        "actors": ["وزارت امور خارجه ایران"],
        "location": None,
        "importance": 3,
        "verification_status": "confirmed",
    }
    base.update(overrides)
    return base


class _Result:
    def __init__(self, items: list[Any]):
        self._items = items

    def all(self) -> list[Any]:
        return self._items


class FakeSession:
    """Session stub; ``source_count`` controls the corroboration lookup."""

    def __init__(self, articles: list[Article], source_count: int = 1):
        self.articles = articles
        self.source_count = source_count
        self.commit_count = 0
        self.added: list[Any] = []
        self.updated: list[Any] = []

    def add(self, obj: Any) -> None:
        self.added.append(obj)

    async def flush(self) -> None:
        return None

    async def scalars(self, stmt: Any) -> _Result:
        return _Result(self.articles)

    async def scalar(self, stmt: Any) -> Any:
        return self.source_count

    async def execute(self, stmt: Any) -> _Result:
        self.updated.append(stmt)
        return _Result([(1, "Source A")])

    async def commit(self) -> None:
        self.commit_count += 1

    async def rollback(self) -> None:
        return None


class _MatchResult:
    def __init__(self, is_new_event: bool = True):
        self.is_new_event = is_new_event
        self.event_id = 1
        self.match_type = "initial"
        self.confidence = 0.9


def _article(article_id: int = 1) -> Article:
    return Article(
        id=article_id,
        source_id=1,
        title="Article",
        original_url="https://example.com/1",
        canonical_url="https://example.com/1",
        original_text="body",
        content_hash="HASH_1",
        status="collected",
        attempt_count=0,
    )


def _patch_matcher(monkeypatch, is_new_event: bool = True) -> None:
    async def fake_match(self, session, article, analysis_data, source_name):
        return _MatchResult(is_new_event=is_new_event)

    monkeypatch.setattr("app.processors.pipeline.EventMatcher.match_or_create_event", fake_match)


@pytest.mark.parametrize(
    "value",
    ["ایران", "Iran", "اسرائیل", "Israel", "غزه", "Gaza", "Türkiye", "Turkey", "عربستان سعودی"],
)
def test_middle_east_countries_recognised(value: str):
    assert is_middle_east_country(value)


@pytest.mark.parametrize("value", ["United States", "China", "Mexico", "Spain", "Germany"])
def test_non_regional_countries_rejected(value: str):
    assert not is_middle_east_country(value)


def test_waterway_location_counts_as_regional_nexus():
    assert has_middle_east_nexus([], "Strait of Hormuz")
    assert has_middle_east_nexus([], "تنگه هرمز")
    assert has_middle_east_nexus([], "Red Sea")
    assert not has_middle_east_nexus([], "New York")


def test_normalization_handles_arabic_letters_and_zero_width_non_joiner():
    assert normalize_place("فلسطين") == "فلسطین"
    assert normalize_place("  IRAN  ") == "iran"
    assert normalize_place("عربستان‌سعودی") == "عربستان سعودی"


@pytest.mark.asyncio
async def test_pipeline_rejects_relevant_but_non_regional_story(monkeypatch):
    _patch_matcher(monkeypatch)
    article = _article()
    session = FakeSession([article])

    count = await pipeline.process_collected_articles(
        session,
        limit=10,
        provider=MockAIProvider(
            canned_response=_payload(countries=["United States"], location="New York", event_title="US housing")
        ),
    )

    assert count == 1
    assert article.status == "rejected"
    assert article.ai_payload is not None


@pytest.mark.asyncio
async def test_pipeline_accepts_persian_country_names(monkeypatch):
    _patch_matcher(monkeypatch)
    article = _article()
    session = FakeSession([article])

    await pipeline.process_collected_articles(
        session, limit=10, provider=MockAIProvider(canned_response=_payload(countries=["فلسطین"], location="غزه"))
    )

    assert article.status == "summarized"


def test_single_source_can_never_be_confirmed():
    assert resolve_verification_status("confirmed", 1) == "unconfirmed"
    assert resolve_verification_status("partially_confirmed", 1) == "unconfirmed"
    assert resolve_verification_status("confirmed", 2) == "confirmed"
    assert resolve_verification_status("unconfirmed", 2) == "partially_confirmed"
    assert resolve_verification_status("disputed", 3) == "disputed"
    assert resolve_verification_status("false", 1) == "false"


@pytest.mark.asyncio
async def test_new_single_source_event_is_stored_as_unconfirmed(monkeypatch):
    _patch_matcher(monkeypatch)
    article = _article()
    session = FakeSession([article], source_count=1)

    await pipeline.process_collected_articles(
        session, limit=10, provider=MockAIProvider(canned_response=_payload(verification_status="confirmed"))
    )

    summaries = [obj for obj in session.added if type(obj).__name__ == "Summary"]
    assert len(summaries) == 1
    assert summaries[0].verification_status == "unconfirmed"
    assert article.ai_payload["verification_status_resolved"] == "unconfirmed"


@pytest.mark.asyncio
async def test_corroborated_event_keeps_confirmed_label(monkeypatch):
    _patch_matcher(monkeypatch)
    article = _article()
    session = FakeSession([article], source_count=2)

    await pipeline.process_collected_articles(
        session, limit=10, provider=MockAIProvider(canned_response=_payload(verification_status="confirmed"))
    )

    summaries = [obj for obj in session.added if type(obj).__name__ == "Summary"]
    assert summaries[0].verification_status == "confirmed"


@pytest.mark.asyncio
async def test_merged_event_refreshes_verification_label(monkeypatch):
    _patch_matcher(monkeypatch, is_new_event=False)
    article = _article()
    session = FakeSession([article], source_count=2)

    await pipeline.process_collected_articles(
        session, limit=10, provider=MockAIProvider(canned_response=_payload(verification_status="unconfirmed"))
    )

    assert not [obj for obj in session.added if type(obj).__name__ == "Summary"]
    assert session.updated, "merged events must refresh the summary verification label"


@pytest.mark.asyncio
async def test_high_impact_single_source_goes_to_admin_only(monkeypatch):
    monkeypatch.setattr(settings, "app_env", "production")
    monkeypatch.setattr(settings, "immediate_publish_enabled", True)
    monkeypatch.setattr(settings, "immediate_publish_delay_seconds", 0.0)
    monkeypatch.setattr(settings, "immediate_publish_max_per_run", 5)
    monkeypatch.setattr(settings, "immediate_publish_min_importance", 3)
    _patch_matcher(monkeypatch)
    publisher = AsyncMock(return_value=[{"message_id": 1, "type": "admin"}])
    monkeypatch.setattr(pipeline, "publish_immediate_analysis", publisher)

    article = _article()
    session = FakeSession([article], source_count=1)

    await pipeline.process_collected_articles(
        session,
        limit=10,
        provider=MockAIProvider(canned_response=_payload(importance=4, verification_status="confirmed")),
    )

    publisher.assert_awaited_once()
    assert publisher.await_args.kwargs["admin_only"] is True


@pytest.mark.asyncio
async def test_low_impact_story_skips_immediate_publish(monkeypatch):
    monkeypatch.setattr(settings, "app_env", "production")
    monkeypatch.setattr(settings, "immediate_publish_enabled", True)
    monkeypatch.setattr(settings, "immediate_publish_delay_seconds", 0.0)
    monkeypatch.setattr(settings, "immediate_publish_min_importance", 3)
    _patch_matcher(monkeypatch)
    publisher = AsyncMock(return_value=[{"message_id": 1, "type": "immediate"}])
    monkeypatch.setattr(pipeline, "publish_immediate_analysis", publisher)

    article = _article()
    session = FakeSession([article], source_count=2)

    await pipeline.process_collected_articles(
        session, limit=10, provider=MockAIProvider(canned_response=_payload(importance=2))
    )

    assert article.status == "summarized"
    publisher.assert_not_called()


def test_new_publish_defaults():
    from app.config import Settings

    fresh = Settings()
    assert fresh.immediate_publish_max_per_run == 5
    assert fresh.immediate_publish_min_importance == 3


@pytest.mark.asyncio
async def test_admin_only_delivery_targets_admin_chat(monkeypatch):
    monkeypatch.setattr(settings, "telegram_channel_id", "-100999")
    monkeypatch.setattr(settings, "telegram_admin_chat_id", 123456)
    monkeypatch.setattr(settings, "immediate_publish_delay_seconds", 0.0)

    class _Message:
        message_id = 7

    class _Bot:
        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []
            self.send_message = AsyncMock(side_effect=self._send)

        async def _send(self, **kwargs: Any) -> _Message:
            self.calls.append(kwargs)
            return _Message()

    bot = _Bot()
    analysis = ImmediateAnalysis(
        event_title="Event",
        headline_fa="تیتر",
        summary_fa="خلاصه",
        source_name="Al Jazeera English",
        source_url="https://example.com/1",
        importance=4,
    )

    sent = await publish_immediate_analysis(analysis, bot=bot, dry_run=False, admin_only=True)

    assert bot.calls[0]["chat_id"] == "123456"
    assert "پیش‌نمایش ادمین" in bot.calls[0]["text"]
    assert sent[0]["type"] == "admin"


# --- Phase P: a regional country must be a party, not a passing mention ---


def test_primary_actor_rejects_incidental_country_mention():
    # An Africa-wide analysis that lists Egypt among many countries.
    assert not is_primary_actor(
        ["مصر", "نیجریه", "کنیا"],
        ["اتحادیه آفریقا", "کمیسیون اقتصادی سازمان ملل"],
        "نایروبی",
    )


def test_primary_actor_accepts_country_named_among_actors():
    assert is_primary_actor(["مصر"], ["وزارت خارجه مصر", "اتحادیه آفریقا"], None)
    assert is_primary_actor(["Iran"], ["Iranian Revolutionary Guard Corps"], None)


def test_primary_actor_accepts_regional_location():
    # The analysis names a city rather than a country; Tehran/Cairo are in scope.
    assert is_primary_actor([], ["حزب‌الله"], "تهران")
    assert is_primary_actor(["مصر"], [], "قاهره")


def test_primary_actor_rejects_when_no_actor_or_location_matches():
    assert not is_primary_actor(["مصر"], [], None)
    assert not is_primary_actor([], ["ناتو"], "بروکسل")


def test_africa_story_with_incidental_egypt_is_rejected_by_pipeline():
    payload = _payload(
        countries=["مصر", "نیجریه"],
        actors=["اتحادیه آفریقا"],
        location="نایروبی",
        headline_fa="گزارش فارن افرز: قاره آفریقا به کانون اصلی فعالیت‌ها تبدیل شد",
    )
    assert has_middle_east_nexus(payload["countries"], payload["location"])
    assert not is_primary_actor(payload["countries"], payload["actors"], payload["location"])
