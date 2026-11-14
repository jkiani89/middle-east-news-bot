"""Phase R-1/R-3/R-5 render-time guards of the daily report.

Covers: the Persian language guard applied to stored text, the independent-source
recount that overrides a stale ``confirmed`` verdict, the 15-item cap, and the
``created_at`` cutoff that keeps pre-guard events out of the bulletin.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.config import settings
from app.reports.daily_report import (
    VERIFICATION_LABELS,
    fetch_report_items,
    report_created_after_cutoff,
)


def _event(
    event_id: int,
    importance: int = 4,
    first_seen_at: datetime | None = None,
    title: str = "Test Strategic Event",
):
    ev = MagicMock()
    ev.id = event_id
    ev.title = title
    ev.event_type = "MIL"
    ev.importance = importance
    ev.first_seen_at = first_seen_at or datetime.now(UTC)
    ev.countries = ["Palestine"]
    ev.actors = ["Israeli military"]
    ev.location = "Khan Younis"
    return ev


def _summary(
    event_id: int,
    headline_fa: str = "حمله هوایی نزدیک بیمارستان ناصر در خان یونس گزارش شد",
    summary_fa: str = "منابع محلی از وقوع انفجار در نزدیکی بیمارستان ناصر خبر دادند و ارتش اسرائیل ادعای حمله را تأیید نکرده است.",
    verification_status: str = "confirmed",
):
    sm = MagicMock()
    sm.event_id = event_id
    sm.headline_fa = headline_fa
    sm.summary_fa = summary_fa
    sm.verification_status = verification_status
    sm.details_fa = {"source": "IRNA", "url": f"https://irna.ir/news/{event_id}"}
    return sm


def _article(article_id: int, source_id: int, url: str):
    art = MagicMock()
    art.id = article_id
    art.source_id = source_id
    art.status = "summarized"
    art.original_url = url
    art.collected_at = datetime.now(UTC)
    return art


def _source(name: str = "IRNA", source_id: int = 1):
    src = MagicMock()
    src.id = source_id
    src.name = name
    return src


def _session(event_rows, article_rows):
    session = AsyncMock()
    event_result = MagicMock()
    event_result.all.return_value = event_rows
    article_result = MagicMock()
    article_result.all.return_value = article_rows
    session.execute.side_effect = [event_result, article_result]
    return session


# ==============================================================================
# R-1: Persian language guard at render time
# ==============================================================================

@pytest.mark.asyncio
async def test_english_headline_is_dropped_and_counted():
    session = _session(
        [
            (_event(1), _summary(1)),
            (
                _event(2),
                _summary(
                    2,
                    headline_fa="Israeli strike hits area near Nasser hospital",
                    summary_fa="The military said the strike targeted fighters near the hospital.",
                ),
            ),
        ],
        [(1, _article(10, 1, "https://irna.ir/1"), _source())],
    )

    _, items, meta = await fetch_report_items(session, report_date=date(2026, 11, 14))

    assert [item["event_id"] for item in items] == [1]
    assert meta["language_rejected_count"] == 1
    assert meta["total_events"] == 1


@pytest.mark.asyncio
async def test_arabic_orthography_summary_is_dropped():
    session = _session(
        [
            (
                _event(3),
                _summary(
                    3,
                    headline_fa="يكيكيكيك مكتبة يكتب",
                    summary_fa="يكيكيكيك مكتبة يكتب مدينة الصهيوني",
                ),
            )
        ],
        [],
    )

    _, items, meta = await fetch_report_items(session, report_date=date(2026, 11, 14))

    assert items == []
    assert meta["language_rejected_count"] == 1
    assert meta["total_events"] == 0


@pytest.mark.asyncio
async def test_persian_summary_passes_the_guard():
    session = _session([(_event(4), _summary(4))], [(4, _article(11, 1, "https://irna.ir/4"), _source())])

    _, items, meta = await fetch_report_items(session, report_date=date(2026, 11, 14))

    assert len(items) == 1
    assert meta["language_rejected_count"] == 0


# ==============================================================================
# R-1: independent-source recount overrides a stale "confirmed"
# ==============================================================================

@pytest.mark.asyncio
async def test_single_source_event_is_downgraded_to_unconfirmed():
    session = _session(
        [(_event(5), _summary(5, verification_status="confirmed"))],
        [(5, _article(20, 1, "https://irna.ir/5"), _source())],
    )

    _, items, _meta = await fetch_report_items(session, report_date=date(2026, 11, 14))

    assert items[0]["distinct_sources_count"] == 1
    assert items[0]["verification_status"] == "unconfirmed"
    assert items[0]["verification_status_fa"] == VERIFICATION_LABELS["unconfirmed"]


@pytest.mark.asyncio
async def test_two_articles_from_one_source_stay_single_source():
    session = _session(
        [(_event(6), _summary(6, verification_status="confirmed"))],
        [
            (6, _article(21, 1, "https://irna.ir/6a"), _source()),
            (6, _article(22, 1, "https://irna.ir/6b"), _source()),
        ],
    )

    _, items, _meta = await fetch_report_items(session, report_date=date(2026, 11, 14))

    assert items[0]["distinct_sources_count"] == 1
    assert items[0]["verification_status"] == "unconfirmed"
    assert items[0]["total_sources_count"] == 2


@pytest.mark.asyncio
async def test_two_independent_sources_keep_confirmed_verdict():
    session = _session(
        [(_event(7), _summary(7, verification_status="confirmed"))],
        [
            (7, _article(23, 1, "https://irna.ir/7"), _source("IRNA", 1)),
            (7, _article(24, 2, "https://reuters.com/7"), _source("Reuters", 2)),
        ],
    )

    _, items, _meta = await fetch_report_items(session, report_date=date(2026, 11, 14))

    assert items[0]["distinct_sources_count"] == 2
    assert items[0]["verification_status"] == "confirmed"
    assert items[0]["verification_status_fa"] == VERIFICATION_LABELS["confirmed"]


# ==============================================================================
# R-3: the 15-item cap
# ==============================================================================

@pytest.mark.asyncio
async def test_report_is_capped_at_fifteen_items():
    now = datetime.now(UTC)
    event_rows = [
        (_event(100 + i, importance=5 if i < 3 else 3, first_seen_at=now - timedelta(hours=i)), _summary(100 + i))
        for i in range(18)
    ]
    article_rows = [(100 + i, _article(200 + i, 1, f"https://irna.ir/{i}"), _source()) for i in range(18)]
    session = _session(event_rows, article_rows)

    _, items, meta = await fetch_report_items(session, report_date=date(2026, 11, 14))

    assert len(items) == settings.max_daily_report_items == 15
    assert meta["events_before_cap"] == 18
    assert meta["max_items"] == 15
    assert meta["total_events"] == 15


# ==============================================================================
# R-5: created_at cutoff
# ==============================================================================

def test_cutoff_parses_the_configured_date():
    with patch.object(settings, "daily_report_min_created_at", "2026-11-13"):
        assert report_created_after_cutoff() == datetime(2026, 11, 13, tzinfo=UTC)


def test_cutoff_is_disabled_by_an_empty_value():
    with patch.object(settings, "daily_report_min_created_at", ""):
        assert report_created_after_cutoff() is None


def test_cutoff_ignores_an_invalid_date():
    with patch.object(settings, "daily_report_min_created_at", "13-11-2026"):
        assert report_created_after_cutoff() is None


@pytest.mark.asyncio
async def test_created_at_filter_is_applied_to_the_event_query():
    captured: dict[str, str] = {}
    empty = MagicMock()
    empty.all.return_value = []

    async def _execute(stmt, *args, **kwargs):
        captured["sql"] = str(stmt)
        return empty

    session = AsyncMock()
    session.execute = _execute

    with patch.object(settings, "daily_report_min_created_at", "2026-11-13"):
        await fetch_report_items(session, report_date=date(2026, 11, 14))

    # events has no created_at column; first_seen_at is its ingestion timestamp.
    assert captured["sql"].count("events.first_seen_at >=") == 2


@pytest.mark.asyncio
async def test_created_at_filter_is_absent_when_disabled():
    captured: dict[str, str] = {}
    empty = MagicMock()
    empty.all.return_value = []

    async def _execute(stmt, *args, **kwargs):
        captured["sql"] = str(stmt)
        return empty

    session = AsyncMock()
    session.execute = _execute

    with patch.object(settings, "daily_report_min_created_at", ""):
        await fetch_report_items(session, report_date=date(2026, 11, 14))

    assert captured["sql"].count("events.first_seen_at >=") == 1
