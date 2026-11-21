"""Report-window completeness: every collected story of the window is published.

Membership is the article collection time inside the half-open window; nothing is
dropped for being parked, single-source, non-regional or not yet summarized.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from unittest.mock import MagicMock

import pytest

from app.config import settings
from app.reports.daily_report import fetch_report_items, report_window
from app.reports.telegraph_report import SUMMARY_MISSING_LABEL, fetch_complete_window_items


def _literal_sql(stmt) -> str:
    """Render a statement with its bound values so window bounds can be asserted."""
    try:
        return str(stmt.compile(compile_kwargs={"literal_binds": True}))
    except Exception:  # pragma: no cover - fall back to the parameterised form
        return str(stmt)


class _Rows:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return list(self._rows)

    def scalars(self):
        return self


class _ScriptedSession:
    """Replays scripted query results and keeps the statements for inspection."""

    def __init__(self, executes=(), scalar_lists=()):
        self._executes = list(executes)
        self._scalar_lists = list(scalar_lists)
        self.statements: list[str] = []

    async def execute(self, stmt):
        self.statements.append(_literal_sql(stmt))
        return self._executes.pop(0) if self._executes else _Rows([])

    async def scalars(self, stmt):
        self.statements.append(_literal_sql(stmt))
        return self._scalar_lists.pop(0) if self._scalar_lists else _Rows([])

    async def scalar(self, stmt):  # noqa: ARG002
        return None


def _event(event_id: int, *, importance: int = 1, countries=None, title=None):
    ev = MagicMock()
    ev.id = event_id
    ev.title = title or f"Event {event_id}"
    ev.importance = importance
    ev.event_type = "MIL"
    ev.first_seen_at = datetime(2026, 11, 14, 5, 0, tzinfo=UTC)
    ev.countries = countries if countries is not None else ["Iran"]
    ev.actors = ["Ministry"]
    ev.location = "Tehran"
    return ev


def _summary(event_id: int, *, headline="تیتر فارسی رویداد", summary="خلاصه فارسی رویداد."):
    sm = MagicMock()
    sm.event_id = event_id
    sm.headline_fa = headline
    sm.summary_fa = summary
    sm.verification_status = "unconfirmed"
    sm.details_fa = {"source": "IRNA", "url": f"https://irna.ir/{event_id}"}
    sm.model_name = "gemini"
    sm.prompt_version = "v2"
    return sm


def _article(
    article_id: int,
    *,
    title: str = "Breaking news headline",
    collected_at: datetime | None = None,
    status: str = "collected",
):
    art = MagicMock()
    art.status = status
    art.id = article_id
    art.source_id = 1
    art.title = title
    art.original_url = f"https://irna.ir/{article_id}"
    art.collected_at = collected_at or datetime(2026, 11, 14, 5, 0, tzinfo=UTC)
    art.published_at = None
    return art


def _source():
    src = MagicMock()
    src.id = 1
    src.name = "IRNA"
    return src


@pytest.mark.asyncio
async def test_membership_uses_the_article_collection_time_window(monkeypatch):
    monkeypatch.setattr(settings, "max_daily_report_items", 1)
    monkeypatch.setattr(settings, "bulletin_require_regional", True)

    events = [
        (_event(1, importance=1), _summary(1)),
        (_event(2, importance=1, countries=["France"]), _summary(2)),
        (_event(3, importance=2), _summary(3, headline="Breaking news out of the region")),
    ]
    articles = [(_event_id, _article(_event_id), _source()) for _event_id in (1, 2, 3)]
    session = _ScriptedSession(
        executes=[_Rows(events), _Rows(articles)],
        scalar_lists=[_Rows([1, 2])],
    )

    _date, items, meta = await fetch_report_items(
        session, report_date=date(2026, 11, 14), slot_hour=8, include_all=True
    )

    assert {item["event_id"] for item in items} == {1, 2, 3}
    assert meta["completeness"] == "all_events"
    assert meta["flagged_events_included"] == 2
    # Parked stories stay in the report, explicitly marked.
    assert [item["review_pending"] for item in items if item["event_id"] in (1, 2)] == [True, True]
    # The language guard warns in this mode; it never drops the story.
    assert meta["language_rejected_count"] == 1
    assert any(item.get("language_warning") for item in items if item["event_id"] == 3)
    # No importance threshold in the composed query.
    assert "events.importance >=" not in session.statements[0].lower()


@pytest.mark.asyncio
async def test_window_bounds_are_half_open_and_come_from_the_slot():
    tz_start, tz_end = report_window(date(2026, 11, 14), 8)[1:]
    session = _ScriptedSession()

    await fetch_report_items(
        session, report_date=date(2026, 11, 14), slot_hour=8, include_all=True
    )

    statement = next(item for item in session.statements if "collected_at" in item)
    assert "articles.collected_at >=" in statement
    assert "articles.collected_at <" in statement
    assert tz_start.strftime("%Y-%m-%d %H:%M:%S") in statement
    assert tz_end.strftime("%Y-%m-%d %H:%M:%S") in statement


@pytest.mark.asyncio
async def test_filtered_bulletin_still_applies_importance_and_the_cap(monkeypatch):
    monkeypatch.setattr(settings, "max_daily_report_items", 1)
    events = [
        (_event(1, importance=5), _summary(1)),
        (_event(2, importance=4), _summary(2)),
    ]
    articles = [(_event_id, _article(_event_id), _source()) for _event_id in (1, 2)]
    session = _ScriptedSession(executes=[_Rows(events), _Rows(articles)])

    _date, items, meta = await fetch_report_items(
        session, report_date=date(2026, 11, 14), slot_hour=8
    )

    assert len(items) == 1
    assert meta["completeness"] == "filtered"
    assert "events.importance >=" in session.statements[0].lower()


@pytest.mark.asyncio
async def test_event_without_summary_is_published_with_its_original_facts():
    event_with_summary = _event(1)
    event_without_summary = _event(2, title="Naval incident near the strait")
    session = _ScriptedSession(
        executes=[
            _Rows([(event_with_summary, _summary(1))]),
            _Rows([(1, _article(1), _source())]),
            # Window articles: the second belongs to an event with no summary.
            _Rows([(_article(1), _source(), 1), (_article(2), _source(), 2)]),
            _Rows([event_without_summary]),
        ],
        scalar_lists=[_Rows([])],
    )

    _date, items, meta = await fetch_complete_window_items(
        session, report_date=date(2026, 11, 14), slot_hour=8
    )

    assert len(items) == 2
    missing = next(item for item in items if item.get("event_id") == 2)
    assert missing["summary_missing"] is True
    assert missing["headline_fa"] == "Naval incident near the strait"
    assert missing["primary_url"] == "https://irna.ir/2"
    assert missing["primary_source"] == "IRNA"
    assert meta["collected_articles"] == 2
    assert meta["articles_represented"] == 2
    assert meta["articles_without_summary"] == 1
    assert meta["news_included"] == 2


@pytest.mark.asyncio
async def test_article_without_any_event_is_published():
    session = _ScriptedSession(
        executes=[
            _Rows([]),
            _Rows([(_article(9, title="Unexplained blast reported"), _source(), None)]),
        ],
        scalar_lists=[_Rows([])],
    )

    _date, items, meta = await fetch_complete_window_items(
        session, report_date=date(2026, 11, 14), slot_hour=8
    )

    assert len(items) == 1
    assert items[0]["summary_missing"] is True
    assert items[0]["headline_fa"] == "Unexplained blast reported"
    assert items[0]["content_quality_fa"] == SUMMARY_MISSING_LABEL
    assert meta["unlinked_articles"] == 1
    assert meta["collected_articles"] == meta["articles_represented"] == 1


@pytest.mark.asyncio
async def test_unanalysed_articles_are_published_and_rejected_ones_are_counted_only():
    """D-9: reviewed-and-rejected news is a count, not a page of raw headlines."""
    session = _ScriptedSession(
        executes=[
            _Rows([]),
            _Rows(
                [
                    (_article(1, title="Still waiting"), _source(), None),
                    (_article(2, title="Too old", status="expired"), _source(), None),
                    (_article(3, title="Turned down", status="rejected"), _source(), None),
                    (_article(4, title="Provider error", status="failed"), _source(), None),
                ]
            ),
        ],
        scalar_lists=[_Rows([])],
    )

    _date, items, meta = await fetch_complete_window_items(
        session, report_date=date(2026, 11, 14), slot_hour=8
    )

    by_title = {item["headline_fa"]: item for item in items}
    assert by_title["Still waiting"]["topic_fa"] == "اخبار پردازش‌نشده"
    assert by_title["Too old"]["topic_fa"] == "اخبار پردازش‌نشده"
    assert "منقضی" in by_title["Too old"]["verification_status_fa"]
    # Reviewed-and-rejected and failed rows must not produce a report item at all.
    assert "Turned down" not in by_title
    assert "Provider error" not in by_title
    assert meta["articles_unprocessed"] == 2
    assert meta["articles_rejected"] == 1
    assert meta["articles_failed"] == 1
    assert meta["articles_by_status"] == {"collected": 1, "expired": 1, "rejected": 1, "failed": 1}
    assert meta["unlinked_articles"] == 2
    assert meta["articles_represented"] == 4


def test_the_unprocessed_section_is_absent_when_nothing_was_unprocessed():
    """D-9: an empty «اخبار پردازش‌نشده» heading is never rendered."""
    from app.editorial.telegraph_topics import group_items_by_telegraph_topic

    item = {
        "event_id": 1,
        "topic_key": "me",
        "topic_keys": ["me"],
        "headline_fa": "خبر",
    }
    keys = [key for key, _name, _items in group_items_by_telegraph_topic([item])]
    assert "unprocessed" not in keys
    assert "not_published" not in keys


@pytest.mark.asyncio
async def test_window_boundary_belongs_only_to_the_later_report():
    """An article collected exactly at 08:00 is in the 20:00 report, never both."""
    early = _ScriptedSession()
    await fetch_report_items(
        early, report_date=date(2026, 11, 14), slot_hour=8, include_all=True
    )
    late = _ScriptedSession()
    await fetch_report_items(
        late, report_date=date(2026, 11, 14), slot_hour=20, include_all=True
    )

    early_sql = next(item for item in early.statements if "collected_at" in item)
    late_sql = next(item for item in late.statements if "collected_at" in item)

    assert "articles.collected_at >= '2026-11-13 20:00:00" in early_sql
    assert "articles.collected_at < '2026-11-14 08:00:00" in early_sql
    assert "articles.collected_at >= '2026-11-14 08:00:00" in late_sql
    assert "articles.collected_at < '2026-11-14 20:00:00" in late_sql
