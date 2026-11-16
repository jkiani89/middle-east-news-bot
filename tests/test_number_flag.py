"""Phase Y-1: pipeline behaviour of the number check (log / flag / off)."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.ai.provider import MockAIProvider
from app.ai.summarizer import NewsAnalysis
from app.config import settings
from app.database.models import Article, Event, Summary
from app.processors import pipeline

UNGROUNDED_HEADLINE = "دست‌کم ۱۲ نفر در حمله کشته شدند"
UNGROUNDED_SUMMARY = "منابع محلی از کشته شدن ۱۲ نفر در این حمله خبر دادند. جزئیات بیشتری منتشر نشده است."
GROUNDED_HEADLINE = "نشست منطقه‌ای در ژنو برگزار شد"
GROUNDED_SUMMARY = "هیئت‌های منطقه‌ای در ژنو درباره آتش‌بس گفت‌وگو کردند و بیانیه‌ای منتشر نشد."


class _Result:
    def __init__(self, items: list[object]):
        self._items = items

    def all(self) -> list[object]:
        return self._items


class FakeSession:
    def __init__(self, articles: list[Article]):
        self.articles = articles
        self.added: list[object] = []
        self.commits = 0
        self.executed: list[object] = []

    def add(self, obj: object) -> None:
        self.added.append(obj)

    async def flush(self) -> None:
        return None

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        return None

    async def scalars(self, stmt: object) -> _Result:
        return _Result(self.articles)

    async def execute(self, stmt: object) -> _Result:
        self.executed.append(stmt)
        return _Result([(1, "Source A")])

    async def scalar(self, stmt: object) -> int:
        return 1


def _article() -> Article:
    return Article(
        id=1,
        source_id=1,
        title="Regional talks resume in Geneva",
        original_text="Delegations met in Geneva to discuss a ceasefire; no statement was issued.",
        original_url="https://example.com/story",
        status="collected",
        attempt_count=0,
        collected_at=datetime.now(UTC),
    )


def _analysis(headline: str, summary: str) -> NewsAnalysis:
    return NewsAnalysis(
        is_relevant=True,
        is_injection_attempt=False,
        categories=["DIP"],
        event_title="Geneva talks",
        headline_fa=headline,
        summary_fa=summary,
        countries=["ایران"],
        actors=["وزارت امور خارجه"],
        location="تهران",
        importance=3,
        verification_status="unconfirmed",
    )


async def _run(monkeypatch: pytest.MonkeyPatch, *, mode: str, analysis: NewsAnalysis, only_new: bool = True):
    monkeypatch.setattr(settings, "number_check_mode", mode)
    monkeypatch.setattr(settings, "immediate_publish_only_new_events", only_new)
    monkeypatch.setattr(pipeline, "analyze_article", AsyncMock(return_value=analysis))
    monkeypatch.setattr(pipeline, "_hydrate_article_body", AsyncMock(return_value=None))
    monkeypatch.setattr(pipeline, "_count_event_sources", AsyncMock(return_value=1))
    monkeypatch.setattr(pipeline, "_refresh_event_perspectives", AsyncMock(return_value=None))
    publish = AsyncMock(return_value=None)
    monkeypatch.setattr(pipeline, "_publish_immediate_safely", publish)
    matcher = SimpleNamespace(
        match_or_create_event=AsyncMock(
            return_value=SimpleNamespace(event_id=10, is_new_event=True, match_type="initial", confidence=0.9)
        )
    )
    monkeypatch.setattr(pipeline, "EventMatcher", lambda: matcher)

    article = _article()
    session = FakeSession([article])
    provider = MockAIProvider(canned_response=analysis.model_dump(mode="json"))
    await pipeline.process_collected_articles(session, provider=provider)
    return article, session, publish


@pytest.mark.asyncio
async def test_flag_mode_marks_article_flagged_for_review(monkeypatch: pytest.MonkeyPatch) -> None:
    article, session, publish = await _run(
        monkeypatch, mode="flag", analysis=_analysis(UNGROUNDED_HEADLINE, UNGROUNDED_SUMMARY)
    )
    assert article.status == "flagged_for_review"
    assert article.ai_payload["number_check"]["blocking"] is True
    assert article.ai_payload["number_check"]["mode"] == "flag"
    publish.assert_not_called()


@pytest.mark.asyncio
async def test_flag_mode_preserves_intelligence_with_a_parked_event(monkeypatch: pytest.MonkeyPatch) -> None:
    _, session, _ = await _run(
        monkeypatch, mode="flag", analysis=_analysis(UNGROUNDED_HEADLINE, UNGROUNDED_SUMMARY)
    )
    events = [obj for obj in session.added if isinstance(obj, Event)]
    summaries = [obj for obj in session.added if isinstance(obj, Summary)]
    assert len(events) == 1
    assert events[0].status == "flagged"
    assert events[0].title.startswith("[FLAGGED: NUM]")
    assert len(summaries) == 1
    assert summaries[0].details_fa["flag"] == "number_mismatch"
    assert summaries[0].details_fa["number_check"]["blocking"] is True


@pytest.mark.asyncio
async def test_flag_mode_never_publishes_even_when_only_new_events_is_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    article, _, publish = await _run(
        monkeypatch,
        mode="flag",
        analysis=_analysis(UNGROUNDED_HEADLINE, UNGROUNDED_SUMMARY),
        only_new=False,
    )
    assert article.status == "flagged_for_review"
    publish.assert_not_called()


@pytest.mark.asyncio
async def test_log_mode_keeps_the_article_publishable(monkeypatch: pytest.MonkeyPatch) -> None:
    article, _, publish = await _run(
        monkeypatch, mode="log", analysis=_analysis(UNGROUNDED_HEADLINE, UNGROUNDED_SUMMARY)
    )
    assert article.status == "summarized"
    assert article.ai_payload["number_check"]["status"] == "mismatch"
    publish.assert_called_once()


@pytest.mark.asyncio
async def test_off_mode_records_a_skipped_report(monkeypatch: pytest.MonkeyPatch) -> None:
    article, _, _ = await _run(
        monkeypatch, mode="off", analysis=_analysis(UNGROUNDED_HEADLINE, UNGROUNDED_SUMMARY)
    )
    assert article.status == "summarized"
    assert article.ai_payload["number_check"]["status"] == "skipped"


@pytest.mark.asyncio
async def test_grounded_numbers_stay_clean(monkeypatch: pytest.MonkeyPatch) -> None:
    article, _, _ = await _run(
        monkeypatch, mode="flag", analysis=_analysis(GROUNDED_HEADLINE, GROUNDED_SUMMARY)
    )
    assert article.status == "summarized"
    assert article.ai_payload["number_check"]["blocking"] is False


@pytest.mark.asyncio
async def test_persian_lint_is_recorded_without_blocking(monkeypatch: pytest.MonkeyPatch) -> None:
    """Phase Y-8: a watchlist word is logged in ai_payload and the article still publishes."""
    article, _, publish = await _run(
        monkeypatch,
        mode="log",
        analysis=_analysis(
            GROUNDED_HEADLINE,
            "در این گزارش «مسرح عملیات» و «مظله امنیتی» توصیف شده است.",
        ),
    )
    assert article.status == "summarized"
    lint = article.ai_payload["persian_lint"]
    assert lint["mode"] == "log"
    assert lint["blocking"] is False
    assert {item["word"] for item in lint["findings"]} == {"مسرح", "مظله"}
    publish.assert_called_once()


@pytest.mark.asyncio
async def test_persian_lint_stays_quiet_on_clean_output(monkeypatch: pytest.MonkeyPatch) -> None:
    article, _, _ = await _run(
        monkeypatch, mode="log", analysis=_analysis(GROUNDED_HEADLINE, GROUNDED_SUMMARY)
    )
    assert article.ai_payload["persian_lint"]["findings"] == []


@pytest.mark.asyncio
async def test_number_check_does_not_add_model_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    """Acceptance Y-1.7: the check is local, so the model is still called once per article."""
    analysis = _analysis(GROUNDED_HEADLINE, GROUNDED_SUMMARY)
    monkeypatch.setattr(settings, "number_check_mode", "flag")
    monkeypatch.setattr(pipeline, "_hydrate_article_body", AsyncMock(return_value=None))
    monkeypatch.setattr(pipeline, "_count_event_sources", AsyncMock(return_value=1))
    monkeypatch.setattr(pipeline, "_refresh_event_perspectives", AsyncMock(return_value=None))
    monkeypatch.setattr(pipeline, "_publish_immediate_safely", AsyncMock(return_value=None))
    monkeypatch.setattr(
        pipeline,
        "EventMatcher",
        lambda: SimpleNamespace(
            match_or_create_event=AsyncMock(
                return_value=SimpleNamespace(event_id=10, is_new_event=True, match_type="initial", confidence=0.9)
            )
        ),
    )
    provider = MockAIProvider(canned_response=analysis.model_dump(mode="json"))
    await pipeline.process_collected_articles(FakeSession([_article()]), provider=provider)

    assert provider.calls_by_purpose["analysis"] == 1
    assert provider.calls_by_purpose["schema_repair"] == 0


@pytest.mark.asyncio
async def test_prefilter_is_recorded_without_changing_the_outcome(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Y-11: the cheap check is stored in ai_payload and costs no extra model call."""
    analysis = _analysis(GROUNDED_HEADLINE, GROUNDED_SUMMARY)
    monkeypatch.setattr(pipeline, "_hydrate_article_body", AsyncMock(return_value=None))
    monkeypatch.setattr(pipeline, "_count_event_sources", AsyncMock(return_value=1))
    monkeypatch.setattr(pipeline, "_refresh_event_perspectives", AsyncMock(return_value=None))
    monkeypatch.setattr(pipeline, "_publish_immediate_safely", AsyncMock(return_value=None))
    monkeypatch.setattr(
        pipeline,
        "EventMatcher",
        lambda: SimpleNamespace(
            match_or_create_event=AsyncMock(
                return_value=SimpleNamespace(
                    event_id=10, is_new_event=True, match_type="initial", confidence=0.9
                )
            )
        ),
    )
    provider = MockAIProvider(canned_response=analysis.model_dump(mode="json"))
    article = _article()

    await pipeline.process_collected_articles(FakeSession([article]), provider=provider)

    assert article.status == "summarized"
    assert article.ai_payload["prefilter"] == {
        "would_reject": True,
        "reason": "no_regional_mention",
    }
    assert provider.calls_by_purpose["analysis"] == 1


def _patch_checker_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the numeric checker raise, as a malformed input would."""

    import app.ai.number_check as number_check_module

    def _raise(**kwargs):
        raise RuntimeError("checker exploded")

    monkeypatch.setattr(number_check_module, "check_numbers", _raise)


class _EmptyReportSession(FakeSession):
    """Session that records the SQL it is asked to run and returns no rows."""

    async def execute(self, stmt: object) -> _Result:
        self.executed.append(stmt)
        return _Result([])

    async def scalar(self, stmt: object) -> int:
        return 0


@pytest.mark.asyncio
async def test_checker_failure_does_not_fail_the_article(monkeypatch: pytest.MonkeyPatch) -> None:
    """Y-1b.1: a checker exception leaves the article on its normal path."""
    _patch_checker_failure(monkeypatch)
    article, _, publish = await _run(
        monkeypatch, mode="log", analysis=_analysis(GROUNDED_HEADLINE, GROUNDED_SUMMARY)
    )
    assert article.status == "summarized"
    assert article.attempt_count == 1  # the ordinary per-run increment, nothing extra
    report = article.ai_payload["number_check"]
    assert report["status"] == "error"
    assert report["blocking"] is False
    assert report["unmatched"] == []
    assert report["error"].startswith("RuntimeError: checker exploded")
    # Y-1b.2: log mode still publishes immediately.
    publish.assert_called_once()


@pytest.mark.asyncio
async def test_checker_failure_in_flag_mode_never_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    """Y-1b.4: an errored report is not a finding."""
    _patch_checker_failure(monkeypatch)
    article, session, _ = await _run(
        monkeypatch, mode="flag", analysis=_analysis(UNGROUNDED_HEADLINE, UNGROUNDED_SUMMARY)
    )
    assert article.status == "summarized"
    assert article.ai_payload["number_check"]["status"] == "error"
    assert not [obj for obj in session.added if isinstance(obj, Event)]


@pytest.mark.asyncio
async def test_checker_failure_does_not_trip_the_breaker(monkeypatch: pytest.MonkeyPatch) -> None:
    """Y-1b.3/Y-1b.5: the batch continues and no extra model call is spent."""
    _patch_checker_failure(monkeypatch)
    analysis = _analysis(GROUNDED_HEADLINE, GROUNDED_SUMMARY)
    monkeypatch.setattr(settings, "number_check_mode", "log")
    monkeypatch.setattr(pipeline, "_hydrate_article_body", AsyncMock(return_value=None))
    monkeypatch.setattr(pipeline, "_count_event_sources", AsyncMock(return_value=1))
    monkeypatch.setattr(pipeline, "_refresh_event_perspectives", AsyncMock(return_value=None))
    monkeypatch.setattr(pipeline, "_publish_immediate_safely", AsyncMock(return_value=None))
    monkeypatch.setattr(
        pipeline,
        "EventMatcher",
        lambda: SimpleNamespace(
            match_or_create_event=AsyncMock(
                return_value=SimpleNamespace(event_id=10, is_new_event=True, match_type="initial", confidence=0.9)
            )
        ),
    )
    provider = MockAIProvider(canned_response=analysis.model_dump(mode="json"))
    second = _article()
    second.id = 2
    session = FakeSession([_article(), second])

    processed = await pipeline.process_collected_articles(session, provider=provider)

    assert pipeline._circuit_breaker_tripped is False
    assert processed == 2
    assert provider.calls_by_purpose["analysis"] == 2  # one per article, no repair calls
    assert provider.calls_by_purpose["schema_repair"] == 0


@pytest.mark.asyncio
async def test_bulletin_query_excludes_flagged_articles() -> None:
    """Acceptance Y-1.4 (bulletin half): the report query filters flagged articles out."""
    from datetime import date

    from app.reports.daily_report import fetch_report_items

    session = _EmptyReportSession([])
    await fetch_report_items(session, date(2026, 11, 15))
    sql = " ".join(
        str(stmt.compile(compile_kwargs={"literal_binds": True})) for stmt in session.executed
    )
    assert "flagged_for_review" in sql
    assert "rejected" in sql
