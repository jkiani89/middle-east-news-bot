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
from app.reports.persian_dates import format_dual_persian_date
from app.reports.telegraph_client import (
    TelegraphContentTooLargeError,
    TelegraphError,
    TelegraphPage,
    build_content,
)
from app.reports.telegraph_pagination import plan_topic_parts
from app.reports.telegraph_report import (
    SUMMARY_MISSING_LABEL,
    TELEGRAPH_COMPLETED,
    TELEGRAPH_PARTIAL,
    build_index_page,
    build_item_block,
    build_topic_page,
    format_admin_report,
    format_public_channel_report,
    normalize_topic_entry,
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
        "topic_fa": "ایران و آمریکا" if topic_key == "ir" else "سایر تحولات",
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
    def __init__(self, fail_titles: set[str] | None = None, existing_pages: list[dict] | None = None):
        self.created: list[tuple[str, str]] = []
        self.edited: list[tuple[str, str]] = []
        self.contents: dict[str, list[dict]] = {}
        self.fail_titles = fail_titles or set()
        self.existing_pages = existing_pages or []
        self.accounts = 0

    async def ensure_token(self) -> str:
        self.accounts += 1
        return "token"

    async def list_pages(self, offset: int = 0, limit: int = 200) -> list[dict]:
        return list(self.existing_pages)[offset : offset + limit]

    async def list_pages_until_end(self):
        """The whole account listing, paged exactly like the real client."""
        pages, offset = [], 0
        while True:
            batch = await self.list_pages(offset=offset, limit=200)
            pages.extend(batch)
            if len(batch) < 200:
                return pages, True
            offset += 200

    async def get_page(self, path: str, return_content: bool = False) -> dict:
        nodes = self.contents.get(path)
        if nodes is None:
            raise TelegraphError(f"page {path} not found")
        payload = {"path": path, "url": f"https://telegra.ph/{path}"}
        if return_content:
            payload["content"] = json.dumps(nodes, ensure_ascii=False)
        return payload

    async def create_page(self, title, content, author_name=None, author_url=None):
        if title in self.fail_titles:
            raise TelegraphError(f"create failed for {title}")
        path = f"p{len(self.created) + 1}"
        self.created.append((path, title))
        self.contents[path] = content
        return TelegraphPage(path=path, url=f"https://telegra.ph/{path}", title=title)

    async def edit_page(self, path, title, content, author_name=None):
        if title in self.fail_titles:
            raise TelegraphError(f"edit failed for {title}")
        self.edited.append((path, title))
        self.contents[path] = content
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
    title, nodes = build_topic_page("ایران و آمریکا", date(2026, 11, 14), 8, items, start, end)
    rendered = _text(nodes)

    assert "ایران و آمریکا" in title
    assert format_dual_persian_date(date(2026, 11, 14)) in title
    assert "خبر نخست ایران" in rendered
    assert "خبر دوم ایران" in rendered
    assert "راستی‌آزمایی" not in rendered
    assert "لینک اصلی خبر" not in rendered
    assert rendered.count('"href": "https://irna.ir/news/1"') == 2
    assert '"children": ["IRNA"]' in rendered
    assert "2" in rendered


def test_topic_page_marks_a_missing_summary_and_keeps_the_source_link():
    item = _item(7, "خبر بدون خلاصه", summary="")
    item.update({"summary_missing": True, "is_insufficient": True})
    start, end = _window()
    _title, nodes = build_topic_page("ایران و آمریکا", date(2026, 11, 14), 8, [item], start, end)
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
        "ir": {"path": "p1", "url": "https://telegra.ph/p1", "name_fa": "ایران و آمریکا", "item_count": 1},
        "ye": {"path": "p2", "url": "https://telegra.ph/p2", "name_fa": "عربستان و یمن", "item_count": 1},
    }
    start, end = _window()
    _title, nodes = build_index_page(
        date(2026, 11, 14), 8, sections, topic_pages, start, end, {}
    )
    rendered = _text(nodes)

    assert "https://telegra.ph/p1" in rendered
    assert "https://telegra.ph/p2" in rendered
    assert "ایران و آمریکا" in rendered
    assert "عربستان و یمن" in rendered
    # The index is a table of contents, not a copy of every story.
    assert "خبر نخست" not in rendered


def test_a_flat_legacy_topic_record_is_read_as_part_one():
    """The record written before pagination is part 1 of today's layout."""
    entry = normalize_topic_entry(
        {"path": "old1", "url": "https://telegra.ph/old1", "name_fa": "ایران و آمریکا", "item_count": 7}
    )

    assert entry["part_count"] == 1
    assert entry["layout_version"] == 1
    assert entry["path"] == "old1"
    assert entry["parts"] == [
        {"part": 1, "path": "old1", "url": "https://telegra.ph/old1", "item_count": 7, "legacy": True}
    ]


def test_a_topic_that_does_not_fit_is_planned_in_several_parts():
    items = [dict(_item(n), summary_fa="خ" * 1200) for n in range(1, 26)]

    parts = plan_topic_parts(items, block_builder=build_item_block, overhead_bytes=900)

    assert len(parts) > 1
    assert sum(part.item_count for part in parts) == len(items)


def test_index_page_omits_system_transparency_and_footer():
    """The public index carries editorial news only; no footnote/warning block."""
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

    assert "یادداشت شفافیت" not in rendered
    assert "🛡" not in rendered
    assert "اخبار پردازش‌نشده" not in rendered


def test_unprocessed_items_are_never_grouped_for_public_telegraph():
    items = [
        _item(1, "خبر تحریریه", topic_key="ir"),
        dict(_item(2, "خبر خام"), topic_key="unprocessed", topic_keys=["unprocessed"], topic_fa="اخبار پردازش‌نشده"),
    ]
    sections = group_items_by_telegraph_topic(items)
    keys = [key for key, _name, _items in sections]

    assert "unprocessed" not in keys
    rendered = _text([
        node
        for _key, _name, topic_items in sections
        for node in build_topic_page(_name, date(2026, 11, 14), 8, topic_items, *_window())[1]
    ])
    assert "خبر خام" not in rendered
    assert "خبر تحریریه" in rendered


def test_public_channel_report_is_the_minimal_template():
    sections = group_items_by_telegraph_topic([_item(1)])
    rep_date = date(2026, 11, 14)
    text = format_public_channel_report(
        rep_date, 8, "https://telegra.ph/report-1", sections, {}
    )

    assert text.startswith("📊 <b>جمع‌بندی ۱۲ ساعته تحولات</b>")
    assert f"نوبت 08:00 • {format_dual_persian_date(rep_date)}" in text
    assert "<b>مهم‌ترین محورهای این نوبت:</b>" in text
    assert "• 🇮🇷/🇺🇸 ایران و آمریکا" in text
    assert '<a href="https://telegra.ph/report-1">مطالعه گزارش کامل و مشروح در Telegraph</a>' in text
    # No counts, page counts, unprocessed row or technical footer in the public post.
    assert "خبر در" not in text
    assert "صفحه" not in text
    assert "اخبار پردازش‌نشده" not in text
    assert "🛡" not in text


def test_admin_report_keeps_counts_pages_and_the_unprocessed_row():
    sections = group_items_by_telegraph_topic([_item(1), _item(2, "خبر دوم")])
    text = format_admin_report(
        date(2026, 11, 14),
        8,
        "https://telegra.ph/report-1",
        sections,
        {"articles_unprocessed": 3},
        topic_pages={"ir": {"url": "https://telegra.ph/p1", "part_count": 2, "parts": [
            {"part": 1, "url": "https://telegra.ph/p1"},
            {"part": 2, "url": "https://telegra.ph/p2"},
        ]}},
    )

    assert "📊 <b>گزارش ۱۲ ساعته تحولات — نوبت 08:00</b>" in text
    assert ": 2 خبر در ۲ صفحه" in text
    assert "• 🗂 اخبار پردازش‌نشده: ۳ خبر" in text
    assert "صفحه اصلی گزارش در Telegraph" in text
    assert "🛡" in text


def test_oversized_page_content_raises_instead_of_dropping_news():
    """The old behaviour trimmed trailing blocks; a lost block is a lost news item."""
    nodes = [{"tag": "p", "children": ["x" * 2000]} for _ in range(60)]

    with pytest.raises(TelegraphContentTooLargeError) as excinfo:
        build_content(nodes)

    assert excinfo.value.size > excinfo.value.limit
    assert excinfo.value.limit == 64 * 1024
    # A page that fits is still serialised verbatim.
    assert json.loads(build_content(nodes[:2])) == nodes[:2]


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
    rep_date = date(2026, 11, 14)
    yemen_title = f"عربستان و یمن — گزارش 08:00 {format_dual_persian_date(rep_date)}"
    first_items = [_item(1, "خبر نخست", topic_key="ir"), _item(2, "خبر دوم", topic_key="ye")]
    failing = _Client(fail_titles={yemen_title})
    await publish_telegraph_report(
        session, report_date=rep_date, slot_hour=8, client=failing, items=first_items
    )
    topic_path = session.row.topic_pages["ir"]["path"]
    index_path = session.row.index_path

    # The retry sees a second story for the topic it already published.
    retry_items = first_items + [_item(3, "خبر تازه", topic_key="ir")]
    retry = _Client()
    result = await publish_telegraph_report(
        session, report_date=rep_date, slot_hour=8, client=retry, items=retry_items
    )

    assert result["created"] == 1  # only the missing topic page
    assert retry.created == [("p1", yemen_title)]
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

    rep_date = date(2026, 11, 14)
    dual_date = format_dual_persian_date(rep_date)
    later = _Client()
    result = await publish_telegraph_report(
        _Session(),
        report_date=rep_date,
        slot_hour=8,
        client=later,
        items=items,
        metadata={},
    )

    assert [title for _path, title in later.created] == [
        f"ایران و آمریکا — گزارش 08:00 {dual_date}",
        f"گزارش 08:00 — {dual_date}",
    ]
    assert result["item_count"] == 1


@pytest.mark.asyncio
async def test_partial_run_creates_only_the_missing_topic_on_retry():
    session = _Session()
    rep_date = date(2026, 11, 14)
    yemen_title = f"عربستان و یمن — گزارش 08:00 {format_dual_persian_date(rep_date)}"
    items = [_item(1, "خبر نخست", topic_key="ir"), _item(2, "خبر دوم", topic_key="ye")]
    failing = _Client(fail_titles={yemen_title})

    first = await publish_telegraph_report(
        session, report_date=rep_date, slot_hour=8, client=failing, items=items
    )

    assert first["status"] == TELEGRAPH_PARTIAL
    # The unrecorded create leaves a recovery marker, not a published page.
    assert set(session.row.topic_pages) == {"ir", "ye"}
    assert session.row.topic_pages["ye"]["parts"][0]["pending_create"]
    assert session.row.topic_pages["ye"]["published_item_count"] == 0
    index_path = session.row.index_path

    retry = _Client()
    second = await publish_telegraph_report(
        session, report_date=rep_date, slot_hour=8, client=retry, items=items
    )

    assert second["status"] == TELEGRAPH_COMPLETED
    assert [title for _path, title in retry.created] == [yemen_title]
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


def test_minimal_channel_message_links_the_index_and_names_every_topic():
    sections = group_items_by_telegraph_topic([_item(1, "خبر نخست")])
    text = format_public_channel_report(
        date(2026, 11, 14),
        8,
        "https://telegra.ph/report-1",
        sections,
        {"window_start": "2026-11-13T20:00:00+00:00", "window_end": "2026-11-14T08:00:00+00:00"},
    )

    assert "https://telegra.ph/report-1" in text
    assert "ایران و آمریکا" in text
    assert "08:00" in text
    # The public message has no verification/technical footer.
    assert "تک‌منبعی" not in text
    assert "🛡" not in text


def test_build_item_block_renders_flags_hyperlinked_source_and_omits_verification():
    item = _item(
        10,
        "توافق میان تهران و واشنگتن",
        source="Associated Press",
        url="https://apnews.com/story/10",
        verification="✅ تأییدشده (چندمنبعی)",
        summary="مذاکرات در سطح وزیران خارجه پیگیری شد.",
    )
    item["countries"] = ["Iran", "United States"]
    item["actors"] = ["ایران", "ایالات متحده آمریکا"]

    nodes = build_item_block(item)
    rendered = _text(nodes)

    assert "🇮🇷🇺🇸 — مذاکرات در سطح وزیران خارجه پیگیری شد." in rendered
    assert "راستی‌آزمایی" not in rendered
    assert "تأییدشده" not in rendered
    assert "لینک اصلی خبر" not in rendered
    assert {
        "tag": "p",
        "children": [
            "📡 منبع: ",
            {"tag": "a", "attrs": {"href": "https://apnews.com/story/10"}, "children": ["Associated Press"]},
        ],
    } in nodes
    assert "🕒 زمان انتشار: " in rendered
    assert "۱۴۰۵/۰۸/۲۳" in rendered


def test_telegraph_item_block_never_adds_turkey_flag_for_kurdistan_region():
    item = _item(
        11,
        "نشست بغداد و اربیل درباره بودجه اقلیم کردستان",
        source="Al Jazeera",
        url="https://aljazeera.net/news/11",
        summary="هیئت دولت عراق و نمایندگان اقلیم کردستان در اربیل درباره سهم بودجه گفت‌وگو کردند.",
    )
    item["countries"] = ["اقلیم کردستان", "Kurdistan Region (KRG)", "کردستان عراق", "عراق", "ترکیه"]
    item["actors"] = ["دولت عراق", "حکومت اقلیم کردستان", "اربیل", "بغداد"]

    nodes = build_item_block(item)
    rendered = _text(nodes)

    assert "🇮🇶 — هیئت دولت عراق" in rendered
    assert "🇹🇷" not in rendered


def test_topic_and_index_pages_display_solar_and_lunar_hijri_dates():
    rep_date = date(2026, 11, 14)
    start, end = _window(rep_date, slot_hour=20)
    items = [_item(1, "خبر نمونه", topic_key="ir")]

    topic_title, topic_nodes = build_topic_page("ایران و آمریکا", rep_date, 20, items, start, end)
    topic_rendered = _text(topic_nodes)
    assert "شنبه ۲۳ آبان ۱۴۰۵ | ۳ جمادی‌الثانی ۱۴۴۸" in topic_title
    assert "📅 تاریخ: شنبه ۲۳ آبان ۱۴۰۵ | ۳ جمادی‌الثانی ۱۴۴۸" in topic_rendered
    assert "۱۴۰۵/۰۸/۲۳" in topic_rendered

    sections = group_items_by_telegraph_topic(items)
    idx_title, idx_nodes = build_index_page(
        rep_date, 20, sections, {"ir": {"url": "https://telegra.ph/p1"}}, start, end, {}
    )
    idx_rendered = _text(idx_nodes)
    assert "شنبه ۲۳ آبان ۱۴۰۵ | ۳ جمادی‌الثانی ۱۴۴۸" in idx_title
    assert "📅 تاریخ: شنبه ۲۳ آبان ۱۴۰۵ | ۳ جمادی‌الثانی ۱۴۴۸" in idx_rendered
    assert "۱۴۰۵/۰۸/۲۳" in idx_rendered

