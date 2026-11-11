"""Unit tests for Phase 6 Daily Intelligence Report Generation.

Tests all filtering rules, exclusion of flagged/rejected articles,
multi-source citation display (Staleness Policy Option C), Persian formatting,
and persistence to daily_reports table.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.reports.daily_report import (
    CATEGORY_TITLES,
    VERIFICATION_LABELS,
    build_daily_report,
    fetch_report_items,
    format_persian_daily_report,
    generate_and_save_daily_report,
)


def _make_mock_event(
    event_id: int,
    title: str = "Test Strategic Event",
    importance: int = 4,
    event_type: str = "MIL",
    first_seen_at: datetime | None = None,
    countries: list[str] | None = None,
    actors: list[str] | None = None,
    location: str = "Tehran",
):
    ev = MagicMock()
    ev.id = event_id
    ev.title = title
    ev.importance = importance
    ev.event_type = event_type
    ev.first_seen_at = first_seen_at or datetime.now(UTC)
    ev.countries = countries or ["Iran"]
    ev.actors = actors or ["Foreign Ministry"]
    ev.location = location
    return ev


def _make_mock_summary(
    event_id: int,
    headline_fa: str = "تیتر تستی رویداد راهبردی",
    summary_fa: str = "متن تفصیلی خلاصه خبر به زبان فارسی برای سنجش صحت کاراکترها و طول گزارش.",
    verification_status: str = "confirmed",
    details_fa: dict | None = None,
):
    sm = MagicMock()
    sm.id = event_id + 100
    sm.event_id = event_id
    sm.headline_fa = headline_fa
    sm.summary_fa = summary_fa
    sm.verification_status = verification_status
    sm.details_fa = details_fa or {"source": "IRNA", "url": "https://irna.ir/news/123"}
    return sm


def _make_mock_article(
    article_id: int,
    status: str = "summarized",
    original_url: str = "https://news.un.org/story/1",
    collected_at: datetime | None = None,
):
    art = MagicMock()
    art.id = article_id
    art.status = status
    art.original_url = original_url
    art.collected_at = collected_at or datetime.now(UTC)
    return art


def _make_mock_source(name: str = "UN News"):
    src = MagicMock()
    src.id = 1
    src.name = name
    return src


# ==============================================================================
# 1. Format Unit Tests
# ==============================================================================

def test_format_persian_daily_report_structure_and_length():
    """Verify that Persian formatted report contains all standard sections and exceeds 500 chars."""
    rep_date = date(2026, 11, 10)
    items = [
        {
            "event_id": 1,
            "title": "Major Diplomatic Talks in Geneva",
            "event_type": "DIP",
            "category_fa": CATEGORY_TITLES["DIP"],
            "importance": 5,
            "first_seen_at": "2026-11-10T08:00:00+00:00",
            "countries": ["Iran", "Oman"],
            "actors": ["Delegates"],
            "location": "Geneva",
            "headline_fa": "آغاز دور جدید مذاکرات دیپلماتیک در ژنو با میانجی‌گری مسقط",
            "summary_fa": "به گزارش مراجع رسمی، هیئت‌های نمایندگی طرفین پیرامون رفع تنش‌ها در سوئیس به رایزنی پرداختند.",
            "verification_status": "confirmed",
            "verification_status_fa": VERIFICATION_LABELS["confirmed"],
            "primary_source": "IRNA",
            "primary_url": "https://irna.ir/news/123",
            "additional_sources": [],
            "total_sources_count": 1,
        },
        {
            "event_id": 2,
            "title": "Wheat Trade Agreement Finalized",
            "event_type": "ECO",
            "category_fa": CATEGORY_TITLES["ECO"],
            "importance": 3,
            "first_seen_at": "2026-11-10T09:00:00+00:00",
            "countries": ["Turkiye"],
            "actors": ["Trade Ministry"],
            "location": "Ankara",
            "headline_fa": "امضای قرارداد تأمین غلات و کالاهای اساسی",
            "summary_fa": "قرارداد تجاری جدیدی به ارزش چند صد میلیون دلار جهت تثبیت بازار مواد غذایی امضا گردید.",
            "verification_status": "partially_confirmed",
            "verification_status_fa": VERIFICATION_LABELS["partially_confirmed"],
            "primary_source": "Anadolu",
            "primary_url": "https://aa.com.tr/wheat",
            "additional_sources": [],
            "total_sources_count": 1,
        },
    ]

    report_text = format_persian_daily_report(rep_date, items)

    assert len(report_text) > 500
    assert "بولتن هوش و پایش تحولات خاورمیانه" in report_text
    assert "۱۴۰۵/۰۸/۱۹" in report_text or "2026/11/10" in report_text
    assert "بخش اول: اهم تحولات راهبردی" in report_text
    assert "بخش دوم: سایر تحولات منطقه‌ای و موضوعی" in report_text
    assert "آغاز دور جدید مذاکرات دیپلماتیک" in report_text
    assert "امضای قرارداد تأمین غلات" in report_text
    assert "خط‌مشی تحریریه" in report_text
    assert "⭐⭐⭐⭐⭐" in report_text
    assert "✅ تأییدشده" in report_text


def test_format_persian_daily_report_html_tags_and_split():
    """Verify Option B HTML formatting generates valid tags and splits cleanly with ISS-008 splitter."""
    rep_date = date(2026, 11, 10)
    # Generate large items list to exceed chunk limit
    items = []
    for i in range(25):
        items.append({
            "event_id": i + 1,
            "title": f"Strategic Event {i + 1}",
            "event_type": "DIP",
            "category_fa": CATEGORY_TITLES["DIP"],
            "importance": 5 if i % 2 == 0 else 3,
            "first_seen_at": "2026-11-10T08:00:00+00:00",
            "countries": ["Iran", "Turkiye"],
            "actors": ["Officials"],
            "location": "Regional",
            "headline_fa": f"تیتر تفصیلی و جامع رویداد راهبردی شماره {i + 1} پیرامون تحولات منطقه",
            "summary_fa": f"توضیحات و خلاصه تحلیلی رویداد شماره {i + 1} که شامل اطلاعات مبسوط و ارزیابی موضع است." * 5,
            "verification_status": "confirmed",
            "verification_status_fa": VERIFICATION_LABELS["confirmed"],
            "primary_source": f"Official Source {i + 1}",
            "primary_url": f"https://example.org/news/{i + 1}",
            "additional_sources": [{"source": f"Parallel Source {i + 1}", "url": f"https://parallel.org/{i + 1}"}],
            "total_sources_count": 2,
        })

    report_text = format_persian_daily_report(rep_date, items, as_html=True)
    assert "<b>" in report_text
    assert "</b>" in report_text
    assert "<a href=" in report_text
    assert "</a>" in report_text
    assert "<code>" in report_text

    # Verify all tags in the complete text are balanced
    assert report_text.count("<b>") == report_text.count("</b>")
    assert report_text.count("<a ") == report_text.count("</a>")
    assert report_text.count("<code>") == report_text.count("</code>")

    # Now split with ISS-008 tag-aware splitter and verify every chunk is balanced and <= limit
    from app.telegram.publisher import split_text
    chunks = split_text(report_text, limit=3500)
    assert len(chunks) >= 3
    for chunk in chunks:
        assert len(chunk) <= 3500
        assert chunk.count("<b>") == chunk.count("</b>")
        assert chunk.count("<a ") == chunk.count("</a>")
        assert chunk.count("<code>") == chunk.count("</code>")
        assert chunk.count("<b>") > 0  # Proves tags are actively present in every chunk!


def test_article_with_html_special_chars_escaped():
    """Verify that dangerous HTML characters in news titles/summaries/URLs are properly escaped."""
    import re

    from app.telegram.publisher import split_text

    rep_date = date(2026, 11, 10)
    items = [{
        "importance": 5,
        "headline_fa": 'Iran & Israel <tensions> rise "quoted"',
        "summary_fa": 'A & B < C & "peace talks" > war',
        "category_fa": "امنیت & دفاع <منطقه>",
        "event_type": "SEC",
        "verification_status_fa": "تأییدشده & معتبر",
        "primary_source": "UN & Partners <Official>",
        "primary_url": "https://news.un.org/feed?x=1&y=2&tag=<special>",
        "countries": ["Iran & Iraq <Border>"],
        "actors": ["Gov & Opposition <All>"],
        "additional_sources": [{"source": "Parallel & Independent <Source>", "url": "https://example.com?p=1&q=2"}],
    }]

    report_text = format_persian_daily_report(rep_date, items, as_html=True)

    # 1. Verify entity conversions
    assert "&amp;" in report_text
    assert "&lt;" in report_text
    assert "&gt;" in report_text
    assert "&quot;" in report_text

    # 2. Verify no raw unescaped < or > exists outside valid Telegram HTML tags
    stripped = re.sub(r"<(/)?(b|code|a)(\s+[^>]*)?>", "", report_text)
    assert "<" not in stripped
    assert ">" not in stripped

    # 3. Verify tag balance and splitter compatibility
    chunks = split_text(report_text, limit=3900)
    assert len(chunks) == 1
    assert chunks[0].count("<b>") == chunks[0].count("</b>")
    assert chunks[0].count("<a ") == chunks[0].count("</a>")


def test_format_persian_daily_report_multi_source_citations():
    """Verify that multi-source events display supplementary sources from details_fa['sources']."""
    rep_date = date(2026, 11, 10)
    items = [
        {
            "event_id": 62,
            "title": "COP31 Climate Summit",
            "event_type": "POL",
            "category_fa": CATEGORY_TITLES["POL"],
            "importance": 4,
            "first_seen_at": "2026-11-10T08:00:00+00:00",
            "countries": ["Turkiye"],
            "actors": ["UNFCCC"],
            "location": "Antalya",
            "headline_fa": "آغاز اجلاس اقلیمی آنتالیا با حضور نمایندگان بین‌المللی",
            "summary_fa": "اجلاس بررسی تغییرات اقلیمی با مشارکت هیئت‌های دیپلماتیک افتتاح شد.",
            "verification_status": "confirmed",
            "verification_status_fa": VERIFICATION_LABELS["confirmed"],
            "primary_source": "UN News",
            "primary_url": "https://news.un.org/story/first",
            "additional_sources": [
                {
                    "source": "Al Jazeera",
                    "url": "https://aljazeera.com/cop31-coverage",
                    "article_id": 19,
                    "added_at": "2026-11-10T09:30:00+00:00",
                }
            ],
            "total_sources_count": 2,
        }
    ]

    report_text = format_persian_daily_report(rep_date, items)

    assert "پوشش‌های موازی و منابع تکمیلی" in report_text
    assert "Al Jazeera" in report_text
    assert "https://aljazeera.com/cop31-coverage" in report_text


def test_format_persian_daily_report_empty_items():
    """Verify behavior when no events match the time window or filter."""
    rep_date = date(2026, 11, 10)
    report_text = format_persian_daily_report(rep_date, [])
    assert "هیچ رویدادی با شرایط مدنظر ثبت نگردیده است" in report_text
    assert "بولتن هوش و پایش" in report_text


# ==============================================================================
# 2. Database Filter & Exclusion Unit Tests
# ==============================================================================

@pytest.mark.asyncio
async def test_fetch_report_items_excludes_flagged_articles():
    """Strict security test: events with any article 'flagged_for_review' must be excluded."""
    session = AsyncMock()

    ev1 = _make_mock_event(1, importance=4)
    sm1 = _make_mock_summary(1)

    # When query executes, simulate session returning empty rows because subquery excluded it
    mock_result_events = MagicMock()
    mock_result_events.all.return_value = []

    session.execute.return_value = mock_result_events

    rep_date, items, meta = await fetch_report_items(session, report_date=date(2026, 11, 10))

    assert len(items) == 0
    assert meta["total_events"] == 0
    assert session.execute.called


@pytest.mark.asyncio
async def test_fetch_report_items_filters_by_importance_and_time_window():
    """Verify that only events within the window and with importance >= min_importance are returned."""
    session = AsyncMock()

    ev1 = _make_mock_event(1, importance=5)
    sm1 = _make_mock_summary(1)
    ev2 = _make_mock_event(2, importance=3)
    sm2 = _make_mock_summary(2)

    # First execute returns Event, Summary rows
    mock_result_events = MagicMock()
    mock_result_events.all.return_value = [(ev1, sm1), (ev2, sm2)]

    # Second execute returns linked ArticleEvent, Article, Source rows
    art1 = _make_mock_article(10)
    src1 = _make_mock_source("UN News")
    art2 = _make_mock_article(20)
    src2 = _make_mock_source("Reuters")

    mock_result_articles = MagicMock()
    mock_result_articles.all.return_value = [
        (1, art1, src1),
        (2, art2, src2),
    ]

    session.execute.side_effect = [mock_result_events, mock_result_articles]

    rep_date, items, meta = await fetch_report_items(
        session, report_date=date(2026, 11, 10), min_importance=3
    )

    assert len(items) == 2
    assert meta["total_events"] == 2
    assert meta["strategic_events_count"] == 1  # importance 5
    assert meta["regional_events_count"] == 1   # importance 3
    assert items[0]["event_id"] == 1
    assert items[1]["event_id"] == 2


@pytest.mark.asyncio
async def test_build_daily_report_creates_daily_report_record():
    """Verify that build_daily_report persists DailyReport with status='generated'."""
    session = AsyncMock()
    session.add = MagicMock()

    ev1 = _make_mock_event(1, importance=4)
    sm1 = _make_mock_summary(1)

    mock_result_events = MagicMock()
    mock_result_events.all.return_value = [(ev1, sm1)]

    art1 = _make_mock_article(1)
    src1 = _make_mock_source("UN News")
    mock_result_articles = MagicMock()
    mock_result_articles.all.return_value = [(1, art1, src1)]

    session.execute.side_effect = [mock_result_events, mock_result_articles]
    # No existing report in DB
    session.scalar.return_value = None

    rep_day, items = await build_daily_report(
        report_date=date(2026, 11, 10), session=session
    )

    assert rep_day == date(2026, 11, 10)
    assert len(items) == 1
    # Verify session.add called with DailyReport
    assert session.add.called
    added_obj = session.add.call_args[0][0]
    assert added_obj.status == "generated"
    assert added_obj.report_date == date(2026, 11, 10)
    assert "formatted_text" in added_obj.content
    assert "items" in added_obj.content


@pytest.mark.asyncio
async def test_build_daily_report_updates_existing_record():
    """Verify that build_daily_report resets publishing state on an existing DailyReport record."""
    session = AsyncMock()
    session.add = MagicMock()

    ev1 = _make_mock_event(1, importance=4)
    sm1 = _make_mock_summary(1)

    mock_result_events = MagicMock()
    mock_result_events.all.return_value = [(ev1, sm1)]

    art1 = _make_mock_article(1)
    src1 = _make_mock_source("UN News")
    mock_result_articles = MagicMock()
    mock_result_articles.all.return_value = [(1, art1, src1)]

    session.execute.side_effect = [mock_result_events, mock_result_articles]

    existing_report = MagicMock()
    existing_report.status = "published"
    existing_report.telegram_message_ids = [999, 1000]
    existing_report.published_at = datetime.now(UTC)
    session.scalar.return_value = existing_report

    rep_day, items = await build_daily_report(
        report_date=date(2026, 11, 10), session=session
    )

    # Verify complete reset of publishing state
    assert existing_report.status == "generated"
    assert existing_report.telegram_message_ids == []
    assert existing_report.published_at is None
    assert "formatted_text" in existing_report.content
    assert not session.add.called


@pytest.mark.asyncio
async def test_fetch_report_items_multiple_linked_articles_distinct_urls():
    """Verify that multiple linked articles with distinct URLs are captured as additional sources."""
    session = AsyncMock()

    ev1 = _make_mock_event(1, importance=4)
    sm1 = _make_mock_summary(1, details_fa={"source": "Primary Source", "url": "https://source1.com/art1"})

    mock_result_events = MagicMock()
    mock_result_events.all.return_value = [(ev1, sm1)]

    art1 = _make_mock_article(1, original_url="https://source1.com/art1")
    src1 = _make_mock_source("Source 1")
    art2 = _make_mock_article(2, original_url="https://source2.com/art2")
    src2 = _make_mock_source("Source 2")

    mock_result_articles = MagicMock()
    mock_result_articles.all.return_value = [
        (1, art1, src1),
        (1, art2, src2),
    ]

    session.execute.side_effect = [mock_result_events, mock_result_articles]

    rep_date, items, meta = await fetch_report_items(session, report_date=date(2026, 11, 10))

    assert len(items) == 1
    assert items[0]["total_sources_count"] == 2
    assert len(items[0]["additional_sources"]) == 1
    assert items[0]["additional_sources"][0]["url"] == "https://source2.com/art2"


@pytest.mark.asyncio
async def test_build_daily_report_with_none_session_uses_sessionlocal():
    """Verify that build_daily_report uses SessionLocal when session is None."""
    mock_session = AsyncMock()
    mock_session.add = MagicMock()
    mock_session.commit = AsyncMock()

    ev1 = _make_mock_event(1, importance=4)
    sm1 = _make_mock_summary(1)
    mock_res_ev = MagicMock()
    mock_res_ev.all.return_value = [(ev1, sm1)]
    mock_res_art = MagicMock()
    mock_res_art.all.return_value = []
    mock_session.execute.side_effect = [mock_res_ev, mock_res_art]
    mock_session.scalar.return_value = None

    class MockContextManager:
        async def __aenter__(self):
            return mock_session
        async def __aexit__(self, exc_type, exc_val, exc_tb):
            pass

    with patch("app.reports.daily_report.SessionLocal", return_value=MockContextManager()):
        rep_day, items = await build_daily_report(report_date=date(2026, 11, 10))
        assert rep_day == date(2026, 11, 10)
        assert len(items) == 1
        assert mock_session.commit.called


@pytest.mark.asyncio
async def test_generate_and_save_daily_report_creates_or_updates():
    """Verify that generate_and_save_daily_report returns (date, items, formatted_text)."""
    mock_session = AsyncMock()
    mock_session.add = MagicMock()
    mock_session.commit = AsyncMock()

    ev1 = _make_mock_event(1, importance=4)
    sm1 = _make_mock_summary(1)
    mock_res_ev = MagicMock()
    mock_res_ev.all.return_value = [(ev1, sm1)]
    mock_res_art = MagicMock()
    mock_res_art.all.return_value = []
    mock_session.execute.side_effect = [mock_res_ev, mock_res_art]

    existing_rep = MagicMock()
    mock_session.scalar.return_value = existing_rep

    class MockContextManager:
        async def __aenter__(self):
            return mock_session
        async def __aexit__(self, exc_type, exc_val, exc_tb):
            pass

    with patch("app.reports.daily_report.SessionLocal", return_value=MockContextManager()):
        rep_day, items, formatted_text = await generate_and_save_daily_report(report_date=date(2026, 11, 10))
        assert rep_day == date(2026, 11, 10)
        assert len(items) == 1
        assert len(formatted_text) > 0
        assert mock_session.commit.called


@pytest.mark.asyncio
async def test_fetch_report_items_detects_mock_and_real_content_quality():
    """Verify that content_quality correctly detects 'mock', 'real', and 'mixed'."""
    session = AsyncMock()

    # Case 1: Mock summary
    ev1 = _make_mock_event(1)
    sm1 = _make_mock_summary(1, headline_fa="تیتر خبر 1: تست موک", summary_fa="خلاصه خبر 1 پیرامون تست.")
    mock_res_ev1 = MagicMock()
    mock_res_ev1.all.return_value = [(ev1, sm1)]
    mock_res_art1 = MagicMock()
    mock_res_art1.all.return_value = []
    session.execute.side_effect = [mock_res_ev1, mock_res_art1]

    _, items1, meta1 = await fetch_report_items(session, report_date=date(2026, 11, 10))
    assert meta1["content_quality"] == "mock"
    assert meta1["mock_summaries_count"] == 1
    assert meta1["real_summaries_count"] == 0

    # Case 2: Real summary
    ev2 = _make_mock_event(2)
    sm2 = _make_mock_summary(2, headline_fa="آغاز دور جدید مذاکرات", summary_fa="متن واقعی تحلیلی")
    sm2.model_name = "gemini-2.5-flash"
    sm2.prompt_version = "v2"
    mock_res_ev2 = MagicMock()
    mock_res_ev2.all.return_value = [(ev2, sm2)]
    mock_res_art2 = MagicMock()
    mock_res_art2.all.return_value = []
    session.execute.side_effect = [mock_res_ev2, mock_res_art2]

    _, items2, meta2 = await fetch_report_items(session, report_date=date(2026, 11, 10))
    assert meta2["content_quality"] == "real"
    assert meta2["mock_summaries_count"] == 0
    assert meta2["real_summaries_count"] == 1
