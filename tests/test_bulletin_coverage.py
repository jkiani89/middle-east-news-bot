"""Tests for bulletin coverage, verification updates, supplementary news, and source counting.

Covers the 8 core operational requirements:
1. Important single-source story is included in daily bulletin.
2. Important unconfirmed story is not excluded.
3. Important supplementary news from an existing Event is reflected in the bulletin.
4. Duplicate news without new information is not re-entered.
5. Current article is included in valid source count.
6. Rejected and quarantined articles are not counted as valid sources.
7. Disputed status is displayed with proper badge.
8. Important news have priority when hitting the max items cap.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.processors import pipeline
from app.reports.daily_report import (
    VERIFICATION_LABELS,
    fetch_report_items,
    format_persian_daily_report,
)


def _mock_event(
    event_id: int,
    importance: int = 4,
    title: str = "تحولات نظامی در منطقه",
    first_seen_at: datetime | None = None,
) -> MagicMock:
    ev = MagicMock()
    ev.id = event_id
    ev.title = title
    ev.event_type = "MIL"
    ev.importance = importance
    ev.countries = ["لبنان", "سوریه"]
    ev.actors = ["ارتش لبنان"]
    ev.location = "جنوب لبنان"
    ev.first_seen_at = first_seen_at or datetime.now(UTC)
    return ev


def _mock_summary(
    event_id: int,
    headline_fa: str = "حمله هوایی به مواضع نظامی در جنوب گزارش شد",
    summary_fa: str = "منابع محلی از وقوع انفجار در پایگاه نظامی خبر دادند.",
    verification_status: str = "unconfirmed",
    sources: list[dict[str, Any]] | None = None,
) -> MagicMock:
    sm = MagicMock()
    sm.event_id = event_id
    sm.headline_fa = headline_fa
    sm.summary_fa = summary_fa
    sm.verification_status = verification_status
    sm.details_fa = {
        "source": "IRNA",
        "url": f"https://irna.ir/news/{event_id}",
        "sources": sources or [
            {
                "source": "IRNA",
                "url": f"https://irna.ir/news/{event_id}",
                "article_id": event_id * 10,
                "added_at": datetime.now(UTC).isoformat(),
                "headline_fa": headline_fa,
                "summary_fa": summary_fa,
            }
        ],
    }
    return sm


def _mock_article(article_id: int, source_id: int, status: str = "summarized", url: str | None = None) -> MagicMock:
    art = MagicMock()
    art.id = article_id
    art.source_id = source_id
    art.status = status
    art.original_url = url or f"https://source{source_id}.com/art{article_id}"
    art.collected_at = datetime.now(UTC)
    art.ai_payload = {}
    return art


def _mock_source(source_id: int, name: str = "IRNA") -> MagicMock:
    src = MagicMock()
    src.id = source_id
    src.name = name
    return src


def _build_session(events_summaries: list[tuple[Any, Any]], article_rows: list[tuple[int, Any, Any]]) -> AsyncMock:
    session = AsyncMock(spec=AsyncSession)
    ev_result = MagicMock()
    ev_result.all.return_value = events_summaries
    art_result = MagicMock()
    art_result.all.return_value = article_rows
    session.execute.side_effect = [ev_result, art_result]
    return session


# ==============================================================================
# Requirement 1: Important single-source story is included in daily bulletin
# ==============================================================================

@pytest.mark.asyncio
async def test_important_single_source_included_in_bulletin():
    ev = _mock_event(1, importance=4)
    sm = _mock_summary(1, verification_status="unconfirmed")
    art = _mock_article(10, source_id=1, status="summarized")
    src = _mock_source(1, "IRNA")

    session = _build_session([(ev, sm)], [(1, art, src)])

    rep_date, items, meta = await fetch_report_items(session, report_date=date(2026, 11, 15))

    assert len(items) == 1
    assert items[0]["event_id"] == 1
    assert items[0]["importance"] == 4
    assert items[0]["verification_status"] == "unconfirmed"
    assert items[0]["verification_status_fa"] == VERIFICATION_LABELS["unconfirmed"]


# ==============================================================================
# Requirement 2: Important unconfirmed story is not excluded
# ==============================================================================

@pytest.mark.asyncio
async def test_important_unconfirmed_story_not_excluded():
    ev = _mock_event(2, importance=5)
    sm = _mock_summary(2, verification_status="unconfirmed")
    art1 = _mock_article(20, source_id=1, status="summarized")
    art2 = _mock_article(21, source_id=2, status="summarized")
    src1 = _mock_source(1, "IRNA")
    src2 = _mock_source(2, "Reuters")

    session = _build_session([(ev, sm)], [(2, art1, src1), (2, art2, src2)])

    _, items, _ = await fetch_report_items(session, report_date=date(2026, 11, 15))

    assert len(items) == 1
    assert items[0]["event_id"] == 2
    assert items[0]["importance"] == 5
    assert items[0]["distinct_sources_count"] == 2


# ==============================================================================
# Requirement 3: Important supplementary news from an existing Event is reflected
# ==============================================================================

def test_important_supplementary_news_reflected_in_bulletin():
    item = {
        "event_id": 3,
        "importance": 4,
        "category_fa": "تحولات نظامی و امنیتی",
        "event_type": "MIL",
        "headline_fa": "حمله موشکی به پایگاه نظامی گزارش شد",
        "summary_fa": "پایگاه نظامی هدف حمله قرار گرفت.",
        "countries": ["سوریه"],
        "actors": ["ارتش سوریه"],
        "location": "دمشق",
        "verification_status": "partially_confirmed",
        "verification_status_fa": "⚠️ تا حدی تأییدشده",
        "primary_source": "IRNA",
        "primary_url": "https://irna.ir/news/3",
        "additional_sources": [
            {
                "source": "Reuters",
                "url": "https://reuters.com/news/3-update",
                "headline_fa": "وزارت دفاع خسارات سنگین به پایگاه را تأیید کرد",
                "summary_fa": "مقامات رسمی خسارات را تأیید کردند.",
            }
        ],
    }

    rendered_html = format_persian_daily_report(
        report_date=date(2026, 11, 15),
        items=[item],
        as_html=True,
    )

    assert "پوشش‌های موازی و منابع تکمیلی" in rendered_html
    assert "وزارت دفاع خسارات سنگین به پایگاه را تأیید کرد" in rendered_html
    assert "Reuters" in rendered_html


# ==============================================================================
# Requirement 4: Duplicate identical news without new information is not re-entered
# ==============================================================================

def test_duplicate_news_without_new_info_not_duplicated():
    item = {
        "event_id": 4,
        "importance": 4,
        "category_fa": "تحولات نظامی",
        "event_type": "MIL",
        "headline_fa": "حمله موشکی به پایگاه نظامی گزارش شد",
        "summary_fa": "پایگاه نظامی هدف قرار گرفت.",
        "countries": ["سوریه"],
        "actors": ["ارتش"],
        "location": "دمشق",
        "verification_status": "confirmed",
        "verification_status_fa": "✅ تأییدشده",
        "primary_source": "IRNA",
        "primary_url": "https://irna.ir/news/4",
        "additional_sources": [
            {
                "source": "SANA",
                "url": "https://sana.sy/news/4",
                "headline_fa": "حمله موشکی به پایگاه نظامی گزارش شد",  # Identical headline
                "summary_fa": "پایگاه نظامی هدف قرار گرفت.",
            }
        ],
    }

    rendered_html = format_persian_daily_report(
        report_date=date(2026, 11, 15),
        items=[item],
        as_html=True,
    )

    # Identical headline should not be duplicated as secondary text
    # It should only show the link to SANA without ": حمله موشکی به پایگاه نظامی گزارش شد"
    assert "SANA" in rendered_html
    # Count occurrences of the headline: should appear exactly once in the rendered report
    assert rendered_html.count("حمله موشکی به پایگاه نظامی گزارش شد") == 1


# ==============================================================================
# Requirement 5: Current article is included in valid source count
# ==============================================================================

@pytest.mark.asyncio
async def test_current_article_included_in_valid_source_count(monkeypatch):
    """Verify that moving article.status='summarized' before flush includes the article in count."""
    from app.ai.provider import MockAIProvider
    from tests.test_editorial_gates import FakeSession, _article, _patch_matcher, _payload

    _patch_matcher(monkeypatch)
    article = _article()
    session = FakeSession([article], source_count=1)

    captured_statuses = []
    real_count = pipeline._count_event_sources

    async def spy_count(sess, eid):
        captured_statuses.append(article.status)
        return await real_count(sess, eid)

    monkeypatch.setattr(pipeline, "_count_event_sources", spy_count)

    await pipeline.process_collected_articles(
        session,
        limit=10,
        provider=MockAIProvider(canned_response=_payload(verification_status="confirmed")),
    )

    assert len(captured_statuses) == 1
    assert captured_statuses[0] == "summarized"


# ==============================================================================
# Requirement 6: Rejected and quarantined articles are not counted as valid sources
# ==============================================================================

@pytest.mark.asyncio
async def test_rejected_and_quarantined_articles_not_counted_as_valid_sources():
    """_count_event_sources must filter by Article.status == 'summarized'."""
    session = AsyncMock()
    session.scalar.return_value = 1

    count = await pipeline._count_event_sources(session, event_id=6)

    assert count == 1
    stmt = session.scalar.call_args[0][0]
    compiled = str(stmt.compile())
    assert "articles.status = :status_1" in compiled or "articles.status =" in compiled


# ==============================================================================
# Requirement 7: Disputed status is displayed with proper badge
# ==============================================================================

def test_disputed_status_displayed_with_proper_badge():
    item = {
        "event_id": 7,
        "importance": 4,
        "category_fa": "تحولات نظامی",
        "event_type": "MIL",
        "headline_fa": "روایت‌های متناقض از عامل انفجار در بندر",
        "summary_fa": "طرفین یکدیگر را متهم کردند.",
        "countries": ["یمن"],
        "actors": ["انصارالله"],
        "location": "الحدیده",
        "verification_status": "disputed",
        "verification_status_fa": "⚖️ مورد مناقشه",
        "primary_source": "Al Masirah",
        "primary_url": "https://almasirah.ye/news/7",
        "additional_sources": [],
    }

    rendered_html = format_persian_daily_report(
        report_date=date(2026, 11, 15),
        items=[item],
        as_html=True,
    )

    assert "⚖️ مورد مناقشه" in rendered_html


# ==============================================================================
# Requirement 8: Important news have priority when hitting the max items cap
# ==============================================================================

@pytest.mark.asyncio
async def test_important_news_prioritized_under_max_items_cap(monkeypatch):
    """When items exceed max_daily_report_items, strategic items (imp >= 4) must be preserved."""
    monkeypatch.setattr(settings, "max_daily_report_items", 10)

    now = datetime.now(UTC)
    events_summaries = []
    article_rows = []

    # 4 strategic events (imp 4 and 5)
    for i in range(4):
        ev = _mock_event(100 + i, importance=5 if i < 2 else 4, first_seen_at=now - timedelta(hours=i))
        sm = _mock_summary(100 + i)
        art = _mock_article(1000 + i, source_id=1, status="summarized")
        src = _mock_source(1, "Reuters")
        events_summaries.append((ev, sm))
        article_rows.append((100 + i, art, src))

    # 15 regional events (imp 3)
    for i in range(15):
        ev = _mock_event(200 + i, importance=3, first_seen_at=now - timedelta(hours=i + 5))
        sm = _mock_summary(200 + i)
        art = _mock_article(2000 + i, source_id=1, status="summarized")
        src = _mock_source(1, "IRNA")
        events_summaries.append((ev, sm))
        article_rows.append((200 + i, art, src))

    session = _build_session(events_summaries, article_rows)

    _, items, meta = await fetch_report_items(session, report_date=date(2026, 11, 15))

    assert len(items) == 10
    assert meta["events_before_cap"] == 19
    # All 4 strategic items must be present in the capped output
    strategic_ids = {100, 101, 102, 103}
    actual_ids = {it["event_id"] for it in items}
    assert strategic_ids.issubset(actual_ids)
    # Remaining 6 slots must be regional items
    regional_in_output = [it for it in items if it["importance"] < 4]
    assert len(regional_in_output) == 6


def test_supplementary_with_same_headline_preserves_new_summary():
    """If supplementary article headline is identical to main headline, but summary has new info, preserve it."""
    item = {
        "event_id": 35,
        "importance": 4,
        "category_fa": "تحولات نظامی و امنیتی",
        "event_type": "MIL",
        "headline_fa": "نشست اضطراری وزرای خارجه در قاهره آغاز شد",
        "summary_fa": "وزرای خارجه کشورهای عربی برای بررسی بحران تشکیل جلسه دادند.",
        "countries": ["مصر"],
        "actors": ["وزرای خارجه"],
        "location": "قاهره",
        "verification_status": "confirmed",
        "verification_status_fa": "✅ تأییدشده",
        "primary_source": "IRNA",
        "primary_url": "https://irna.ir/news/35",
        "additional_sources": [
            {
                "source": "Al Jazeera",
                "url": "https://aljazeera.net/news/35",
                "headline_fa": "نشست اضطراری وزرای خارجه در قاهره آغاز شد",  # Same headline
                "summary_fa": "منابع دیپلماتیک اعلام کردند بیانیه مشترک امشب صادر خواهد شد.",  # New info
            }
        ],
    }

    rendered_html = format_persian_daily_report(
        report_date=date(2026, 11, 15),
        items=[item],
        as_html=True,
    )

    # The new summary info must be rendered
    assert "منابع دیپلماتیک اعلام کردند بیانیه مشترک امشب صادر خواهد شد." in rendered_html
    # The duplicate headline should not be repeated after Al Jazeera
    assert "Al Jazeera" in rendered_html


@pytest.mark.asyncio
async def test_all_strategic_news_preserved_when_exceeding_max_items(monkeypatch):
    """When importance 4 and 5 items exceed max_daily_report_items, ALL of them must remain."""
    monkeypatch.setattr(settings, "max_daily_report_items", 5)

    now = datetime.now(UTC)
    events_summaries = []
    article_rows = []

    # 8 strategic events (imp 4 and 5)
    for i in range(8):
        ev = _mock_event(300 + i, importance=5 if i % 2 == 0 else 4, first_seen_at=now - timedelta(hours=i))
        sm = _mock_summary(300 + i)
        art = _mock_article(3000 + i, source_id=1, status="summarized")
        src = _mock_source(1, "Reuters")
        events_summaries.append((ev, sm))
        article_rows.append((300 + i, art, src))

    # 10 regional events (imp 3)
    for i in range(10):
        ev = _mock_event(400 + i, importance=3, first_seen_at=now - timedelta(hours=i + 10))
        sm = _mock_summary(400 + i)
        art = _mock_article(4000 + i, source_id=1, status="summarized")
        src = _mock_source(1, "IRNA")
        events_summaries.append((ev, sm))
        article_rows.append((400 + i, art, src))

    session = _build_session(events_summaries, article_rows)

    _, items, meta = await fetch_report_items(session, report_date=date(2026, 11, 15))

    # All 8 strategic items must remain even though max_daily_report_items is 5!
    assert len(items) == 8
    assert all(it["importance"] >= 4 for it in items)
    # 0 regional items remain because cap limited the lower-importance news
    assert not any(it["importance"] < 4 for it in items)


# ==============================================================================
# Requirement 9: False status is not published as credible news
# ==============================================================================

@pytest.mark.asyncio
async def test_false_status_not_published_as_credible_news():
    """Verify that a debunked (false) story is not published as credible news.

    1. Low-importance false stories are excluded entirely from bulletin items.
    2. High-importance false stories are framed explicitly as retractions/debunked claims, not credible news.
    3. Disputed stories are included with their dispute badge.
    """
    # 1. Low-importance false event (importance 3)
    ev_low_false = _mock_event(501, importance=3, title="ادعای کذب درباره سقوط بالگرد")
    sm_low_false = _mock_summary(501, verification_status="false")
    art_low = _mock_article(5010, source_id=1, status="summarized")
    src_low = _mock_source(1, "Telegram")

    # 2. High-importance false event (importance 5)
    ev_high_false = _mock_event(502, importance=5, title="شایعه وقوع انفجار در تأسیسات هسته‌ای")
    sm_high_false = _mock_summary(502, headline_fa="شایعه وقوع انفجار در تأسیسات هسته‌ای", verification_status="false")
    art_high = _mock_article(5020, source_id=2, status="summarized")
    src_high = _mock_source(2, "Reuters")

    # 3. Disputed event (importance 4)
    ev_disputed = _mock_event(503, importance=4, title="ادعای ترور یک مقام نظامی")
    sm_disputed = _mock_summary(503, headline_fa="ادعای ترور یک مقام نظامی", verification_status="disputed")
    art_disp = _mock_article(5030, source_id=3, status="summarized")
    src_disp = _mock_source(3, "Al Arabiya")

    session = _build_session(
        [(ev_low_false, sm_low_false), (ev_high_false, sm_high_false), (ev_disputed, sm_disputed)],
        [(501, art_low, src_low), (502, art_high, src_high), (503, art_disp, src_disp)],
    )

    _, items, _ = await fetch_report_items(session, report_date=date(2026, 11, 15))

    item_ids = {it["event_id"] for it in items}
    # Low-importance false is excluded
    assert 501 not in item_ids
    # High-importance false is kept (for retraction/debunking notice)
    assert 502 in item_ids
    # Disputed is kept
    assert 503 in item_ids

    rendered = format_persian_daily_report(
        report_date=date(2026, 11, 15),
        items=items,
        as_html=True,
    )

    # High-importance false is NOT rendered as credible news; it has retraction title and warning
    assert "[تکذیبیه / ادعای ردشده] شایعه وقوع انفجار در تأسیسات هسته‌ای" in rendered
    assert "فاقد اعتبار خبری است" in rendered
    assert "❌ نادرست / ردشده" in rendered

    # Disputed has its badge
    assert "⚖️ مورد مناقشه" in rendered
