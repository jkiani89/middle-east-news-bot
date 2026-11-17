"""Telegraph report: one page per topic, an index linking them, and resumable state."""

from __future__ import annotations

import json
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from app.config import settings
from app.editorial.telegraph_topics import (
    group_items_by_telegraph_topic,
    load_telegraph_topics,
)
from app.reports.telegraph_client import TelegraphError, TelegraphPage, build_content
from app.reports.telegraph_report import (
    SUMMARY_MISSING_LABEL,
    TELEGRAPH_COMPLETED,
    TELEGRAPH_PARTIAL,
    build_index_page,
    build_topic_page,
    format_telegram_notification,
    notification_ready,
    publish_telegraph_report,
)


def _window(report_date: date = date(2026, 11, 14), slot_hour: int = 8):
    tz = ZoneInfo(settings.timezone)
    if slot_hour == 8:
        return (
            datetime(report_date.year, report_date.month, report_date.day, 8, 0, tzinfo=tz),
            datetime(report_date.year, report_date.month, report_date.day, 8, 0, tzinfo=tz),
        )
    return (
        datetime(report_date.year, report_date.month, report_date.day, 8, 0, tzinfo=tz),
        datetime(report_date.year, report_date.month, report_date.day, 20, 0, tzinfo=tz),
    )


def _item(
    event_id: int = 1,
    headline: str = "تیتر خبری رویداد",
    *,
    topic_key: str | None = "ir",
    source: str = "IRNA",
    url: str = "https://irna.ir/news/1",
    verification: str = "❓ تأییدنشده (تک‌منبعی)",
    summary: str = "خلاصه تولیدشده توسط سامانه برای این رویداد.",
) -> dict:
    return {
        "event_id": event_id,
        "topic_key": topic_key,
        "topic_fa": "ایران و مذاکرات" if topic_key == "ir" else "سایر تحولات",
        "headline_fa": headline,
        "summary_fa": summary,
        "primary_source": source,
        "primary_url": url,
        "first_seen_at": "2026-11-14T07:30:00+00:00",
        "verification_status": "unconfirmed",
        "verification_status_fa": verification,
        "additional_sources": [],
    }


def _text(nodes: list[dict]) -> str:
    return json.dumps(nodes, ensure_ascii=False)


class _Session:
    """Minimal session for a single slot row."""

    def __init__(self, row=None):
        self.row = row
        self.commits = 0

    async def scalar(self, stmt):  # noqa: ARG002 - only the slot lookup is used here
        return self.row

    def add(self, obj):
        self.row = obj

    async def commit(self):
        self.commits += 1


class _Client:
    def __init__(self, fail_titles: set[str] | None = None):
        self.created: list[tuple[str, str]] = []
        self.edited: list[tuple[str, str]] = []
        self.fail_titles = fail_titles or set()
        self.accounts = 0

    async def ensure_token(self) -> str:
        self.accounts += 1
        return "token"

    async def create_page(self, title, content, author_name=None, author_url=None):
        if title in self.fail_titles:
            raise TelegraphError(f"create failed for {title}")
        path = f"p{len(self.created) + 1}"
        self.created.append((path, title))
        return TelegraphPage(path=path, url=f"https://telegra.ph/{path}", title=title)

    async def edit_page(self, path, title, content, author_name=None):
        if title in self.fail_titles:
            raise TelegraphError(f"edit failed for {title}")
        self.edited.append((path, title))
        return TelegraphPage(path=path, url=f"https://telegra.ph/{path}", title=title)


def _topic_total(items: list[dict]) -> int:
    return sum(
        len(topic_items)
        for _key, _name, topic_items in group_items_by_telegraph_topic(items)
    )


def test_every_item_lands_in_exactly_one_topic():
    items = [
        _item(1, topic_key="ir"),
        _item(2, topic_key="ye"),
        _item(3, topic_key=None),
        _item(4, topic_key="unknown-topic"),
    ]
    sections = group_items_by_telegraph_topic(items)

    assert _topic_total(items) == len(items)
    keys = [key for key, _name, _items in sections]
    assert keys[0] == "ir"
    assert keys[-1] == "other"


def test_telegraph_topic_file_is_loadable():
    topics = load_telegraph_topics()
    assert any(topic.key == "other" for topic in topics)
    assert any(topic.key == "ir" for topic in topics)


def test_topic_page_holds_all_items_of_that_topic():
    items = [
        _item(1, "خبر نخست ایران", topic_key="ir"),
        _item(2, "خبر دوم ایران", topic_key="ir"),
    ]
    start, end = _window()
    title, nodes = build_topic_page("ایران و مذاکرات", date(2026, 11, 14), 8, items, start, end)
    rendered = _text(nodes)

    assert "ایران و مذاکرات" in title
    assert "خبر نخست ایران" in rendered
    assert "خبر دوم ایران" in rendered
    assert rendered.count("لینک اصلی خبر") == 2
    assert "2" in rendered


def test_topic_page_marks_a_missing_summary_and_keeps_the_source_link():
    item = _item(7, "خبر بدون خلاصه", summary="")
    item.update({"summary_missing": True, "is_insufficient": True})
    start, end = _window()
    _title, nodes = build_topic_page("ایران و مذاکرات", date(2026, 11, 14), 8, [item], start, end)
    rendered = _text(nodes)

    assert SUMMARY_MISSING_LABEL in rendered
    assert "https://irna.ir/news/1" in rendered
    assert "IRNA" in rendered


def test_index_page_links_topic_pages_not_individual_news():
    items = [
        _item(1, "خبر نخست", topic_key="ir"),
        _item(2, "خبر دوم", topic_key="ye"),
    ]
    sections = group_items_by_telegraph_topic(items)
    topic_pages = {
        "ir": {"path": "p1", "url": "https://telegra.ph/p1", "name_fa": "ایران و مذاکرات", "item_count": 1},
        "ye": {"path": "p2", "url": "https://telegra.ph/p2", "name_fa": "یمن و دریای سرخ", "item_count": 1},
    }
    start, end = _window()
    _title, nodes = build_index_page(
        date(2026, 11, 14), 8, sections, topic_pages, start, end, {}
    )
    rendered = _text(nodes)

    assert "https://telegra.ph/p1" in rendered
    assert "https://telegra.ph/p2" in rendered
    assert "ایران و مذاکرات" in rendered
    assert "یمن و دریای سرخ" in rendered
    # The index is a table of contents, not a copy of every story.
    assert "خبر نخست" not in rendered


def test_index_page_reports_missing_summary_transparency():
    sections = group_items_by_telegraph_topic([_item(1)])
    start, end = _window()
    _title, nodes = build_index_page(
        date(2026, 11, 14),
        8,
        sections,
        {"ir": {"url": "https://telegra.ph/p1"}},
        start,
        end,
        {"articles_without_summary": 2, "flagged_events_included": 1},
    )
    rendered = _text(nodes)

    assert "۲" in rendered or "2" in rendered
    assert SUMMARY_MISSING_LABEL in rendered
    assert "بازبینی تحریریه" in rendered


def test_oversized_page_content_is_trimmed_below_the_telegraph_cap():
    nodes = [{"tag": "p", "children": ["x" * 2000]} for _ in range(60)]
    serialised = build_content(nodes)
    assert len(serialised.encode("utf-8")) <= 64 * 1024
    assert json.loads(serialised)


@pytest.mark.asyncio
async def test_publish_creates_one_page_per_topic_plus_the_index():
    session = _Session()
    client = _Client()
    items = [
        _item(1, "خبر نخست", topic_key="ir"),
        _item(2, "خبر دوم", topic_key="ye"),
        _item(3, "خبر سوم", topic_key="ir"),
    ]

    result = await publish_telegraph_report(
        session,
        report_date=date(2026, 11, 14),
        slot_hour=8,
        client=client,
        items=items,
        metadata={"content_quality": "real"},
    )

    assert result["status"] == TELEGRAPH_COMPLETED
    assert len(client.created) == 3  # two topics + index
    assert set(session.row.topic_pages) == {"ir", "ye"}
    assert session.row.topic_pages["ir"]["item_count"] == 2
    assert notification_ready(result)


@pytest.mark.asyncio
async def test_a_complete_report_is_not_rebuilt():
    session = _Session()
    first = _Client()
    items = [_item(1, "خبر نخست")]
    await publish_telegraph_report(
        session, report_date=date(2026, 11, 14), slot_hour=8, client=first, items=items
    )

    second = _Client()
    result = await publish_telegraph_report(
        session, report_date=date(2026, 11, 14), slot_hour=8, client=second, items=items
    )

    assert result["skipped"] is True
    assert second.created == []
    assert second.edited == []


@pytest.mark.asyncio
async def test_retry_updates_the_topic_page_instead_of_duplicating_it():
    """A partial report edits the topic page it already published; it never adds a copy."""
    session = _Session()
    first_items = [_item(1, "خبر نخست", topic_key="ir"), _item(2, "خبر دوم", topic_key="ye")]
    failing = _Client(fail_titles={"یمن و دریای سرخ — گزارش 08:00 2026-11-14"})
    await publish_telegraph_report(
        session, report_date=date(2026, 11, 14), slot_hour=8, client=failing, items=first_items
    )
    topic_path = session.row.topic_pages["ir"]["path"]
    index_path = session.row.index_path

    # The retry sees a second story for the topic it already published.
    retry_items = first_items + [_item(3, "خبر تازه", topic_key="ir")]
    retry = _Client()
    result = await publish_telegraph_report(
        session, report_date=date(2026, 11, 14), slot_hour=8, client=retry, items=retry_items
    )

    assert result["created"] == 1  # only the missing topic page
    assert retry.created == [("p1", "یمن و دریای سرخ — گزارش 08:00 2026-11-14")]
    assert [path for path, _title in retry.edited] == [topic_path, index_path]
    assert session.row.topic_pages["ir"]["path"] == topic_path
    assert session.row.topic_pages["ir"]["item_count"] == 2


@pytest.mark.asyncio
async def test_a_story_from_the_previous_report_does_not_remove_this_windows_story():
    """Membership comes from the window, never from what an earlier report published."""
    session = _Session()
    items = [_item(1, "خبر بازه فعلی", topic_key="ir")]
    await publish_telegraph_report(
        session, report_date=date(2026, 11, 13), slot_hour=20, client=_Client(), items=items
    )

    later = _Client()
    result = await publish_telegraph_report(
        _Session(),
        report_date=date(2026, 11, 14),
        slot_hour=8,
        client=later,
        items=items,
        metadata={},
    )

    assert [title for _path, title in later.created] == [
        "ایران و مذاکرات — گزارش 08:00 2026-11-14",
        "گزارش 08:00 — 2026-11-14",
    ]
    assert result["item_count"] == 1


@pytest.mark.asyncio
async def test_partial_run_creates_only_the_missing_topic_on_retry():
    session = _Session()
    items = [_item(1, "خبر نخست", topic_key="ir"), _item(2, "خبر دوم", topic_key="ye")]
    failing = _Client(fail_titles={"یمن و دریای سرخ — گزارش 08:00 2026-11-14"})

    first = await publish_telegraph_report(
        session, report_date=date(2026, 11, 14), slot_hour=8, client=failing, items=items
    )

    assert first["status"] == TELEGRAPH_PARTIAL
    assert set(session.row.topic_pages) == {"ir"}
    index_path = session.row.index_path

    retry = _Client()
    second = await publish_telegraph_report(
        session, report_date=date(2026, 11, 14), slot_hour=8, client=retry, items=items
    )

    assert second["status"] == TELEGRAPH_COMPLETED
    assert [title for _path, title in retry.created] == [
        "یمن و دریای سرخ — گزارش 08:00 2026-11-14"
    ]
    assert retry.edited and retry.edited[0][0] == index_path
    assert set(session.row.topic_pages) == {"ir", "ye"}


@pytest.mark.asyncio
async def test_force_edits_existing_pages_instead_of_creating_duplicates():
    session = _Session()
    items = [_item(1, "خبر نخست")]
    await publish_telegraph_report(
        session, report_date=date(2026, 11, 14), slot_hour=8, client=_Client(), items=items
    )
    paths = {key: entry["path"] for key, entry in session.row.topic_pages.items()}

    forced = _Client()
    await publish_telegraph_report(
        session,
        report_date=date(2026, 11, 14),
        slot_hour=8,
        client=forced,
        items=items,
        force=True,
    )

    assert forced.created == []
    assert [path for path, _title in forced.edited[: len(paths)]] == list(paths.values())


@pytest.mark.asyncio
async def test_client_failure_marks_the_report_failed_and_keeps_no_index():
    class _BrokenClient(_Client):
        async def ensure_token(self) -> str:
            raise TelegraphError("telegraph is unreachable")

    session = _Session()
    result = await publish_telegraph_report(
        session,
        report_date=date(2026, 11, 14),
        slot_hour=8,
        client=_BrokenClient(),
        items=[_item(1)],
    )

    assert result["status"] == "failed"
    assert result["index_url"] == ""
    assert notification_ready(result) is False


def test_notification_links_the_index_and_names_every_topic():
    sections = group_items_by_telegraph_topic([_item(1, "خبر نخست")])
    text = format_telegram_notification(
        date(2026, 11, 14),
        8,
        "https://telegra.ph/report-1",
        sections,
        {"window_start": "2026-11-13T20:00:00+00:00", "window_end": "2026-11-14T08:00:00+00:00"},
    )

    assert "https://telegra.ph/report-1" in text
    assert "ایران و مذاکرات" in text
    assert "08:00" in text
    assert "تک‌منبعی" in text
