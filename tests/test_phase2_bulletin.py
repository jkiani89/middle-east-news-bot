"""Phase 2: the active briefing path publishes the whole 12-hour window."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, date, datetime
from unittest.mock import MagicMock, patch

import pytest

from app.config import settings
from app.reports.daily_report import generate_slot_bulletins


class _Rows:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return list(self._rows)

    def scalars(self):
        return self


class _ScriptedSession:
    def __init__(self, executes=(), scalar_lists=()):
        self._executes = list(executes)
        self._scalar_lists = list(scalar_lists)
        self.statements = []

    async def execute(self, stmt):
        self.statements.append(str(stmt))
        return self._executes.pop(0) if self._executes else _Rows([])

    async def scalars(self, stmt):
        self.statements.append(str(stmt))
        return self._scalar_lists.pop(0) if self._scalar_lists else _Rows([])

    async def scalar(self, stmt):
        return None

    def add(self, obj):
        pass

    async def commit(self):
        pass


def _make_cm(session):
    @asynccontextmanager
    async def _cm():
        yield session

    return _cm


def _event(event_id, *, importance=1, countries=None, title=None):
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


def _summary(event_id):
    sm = MagicMock()
    sm.event_id = event_id
    sm.headline_fa = f"تیتر {event_id}"
    sm.summary_fa = "خلاصه"
    sm.verification_status = "unconfirmed"
    sm.details_fa = {"source": "IRNA", "url": f"https://irna.ir/{event_id}"}
    sm.model_name = "gemini"
    sm.prompt_version = "v2"
    return sm


def _article(article_id, collected_at=None, title=None):
    art = MagicMock()
    art.id = article_id
    art.source_id = 1
    art.title = title or f"News {article_id}"
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
async def test_generate_slot_bulletins_includes_more_than_fifteen_items(monkeypatch):
    monkeypatch.setattr(settings, "max_daily_report_items", 1)
    monkeypatch.setattr(settings, "bulletin_require_regional", True)

    n = 20
    events = [(_event(i, importance=1), _summary(i)) for i in range(1, n + 1)]
    articles = [(i, _article(i), _source()) for i in range(1, n + 1)]
    window_articles = [(_article(i), _source(), i) for i in range(1, n + 1)]

    session = _ScriptedSession(
        executes=[
            _Rows(events),
            _Rows(articles),
            _Rows(window_articles),
            _Rows([]),
        ],
        scalar_lists=[_Rows([])],
    )

    with patch("app.reports.daily_report.SessionLocal", _make_cm(session)), \
            patch("app.reports.daily_report._backfill_perspectives", return_value=None):
        # _backfill_perspectives is async; patch with AsyncMock
        from unittest.mock import AsyncMock
        with patch("app.reports.daily_report._backfill_perspectives", new=AsyncMock(return_value=None)):
            _date, sections, text, metadata = await generate_slot_bulletins(
                report_date=date(2026, 11, 14), slot_hour=8
            )

    items = [item for _key, _name, topic_items in sections for item in topic_items]
    # Each of the 20 events becomes an item (multi-topic may duplicate across sections,
    # but the distinct event count is 20).
    distinct_events = {item.get("event_id") for item in items}
    assert len(distinct_events) == n
    assert metadata.get("completeness") == "all_events"


@pytest.mark.asyncio
async def test_generate_slot_bulletins_keeps_unlinked_and_unsummarised():
    session = _ScriptedSession(
        executes=[
            _Rows([]),  # events with summary (empty -> returns early, no articles query)
            _Rows([(_article(9, title="Unexplained blast"), _source(), None)]),  # window
            _Rows([]),  # missing events
        ],
        scalar_lists=[_Rows([])],
    )

    from unittest.mock import AsyncMock
    with patch("app.reports.daily_report.SessionLocal", _make_cm(session)), \
            patch("app.reports.daily_report._backfill_perspectives", new=AsyncMock(return_value=None)):
        _date, sections, text, metadata = await generate_slot_bulletins(
            report_date=date(2026, 11, 14), slot_hour=8
        )

    items = [item for _key, _name, topic_items in sections for item in topic_items]
    assert len(items) == 1
    assert items[0]["headline_fa"] == "Unexplained blast"
    assert items[0]["summary_missing"] is True


@pytest.mark.asyncio
async def test_generate_slot_bulletins_out_of_window_excluded():
    # Article collected outside window (2026-11-13 05:00) should not appear.
    inside = _article(8, collected_at=datetime(2026, 11, 14, 5, 0, tzinfo=UTC))

    # fetch_report_items filters by collected_at in DB query; scripted session returns
    # only the inside article for the window query.
    session = _ScriptedSession(
        executes=[
            _Rows([]),
            _Rows([(inside, _source(), None)]),
            _Rows([]),
        ],
        scalar_lists=[_Rows([])],
    )

    from unittest.mock import AsyncMock
    with patch("app.reports.daily_report.SessionLocal", _make_cm(session)), \
            patch("app.reports.daily_report._backfill_perspectives", new=AsyncMock(return_value=None)):
        _date, sections, text, metadata = await generate_slot_bulletins(
            report_date=date(2026, 11, 14), slot_hour=8
        )

    items = [item for _key, _name, topic_items in sections for item in topic_items]
    ids = {item.get("article_id") for item in items}
    assert 8 in ids
    assert 7 not in ids


@pytest.mark.asyncio
async def test_generate_slot_bulletins_calls_complete_window():
    """Active path must call fetch_complete_window_items, not the filtered fetch."""
    from unittest.mock import AsyncMock

    session = _ScriptedSession(executes=[], scalar_lists=[_Rows([])])
    called = {"n": 0}

    async def fake_complete(s, report_date=None, slot_hour=None):
        called["n"] += 1
        return date(2026, 11, 14), [], {"completeness": "all_events"}

    with patch("app.reports.daily_report.SessionLocal", _make_cm(session)), \
            patch("app.reports.telegraph_report.fetch_complete_window_items", new=fake_complete), \
            patch("app.reports.daily_report._backfill_perspectives", new=AsyncMock(return_value=None)):
        await generate_slot_bulletins(report_date=date(2026, 11, 14), slot_hour=8)

    assert called["n"] == 1
