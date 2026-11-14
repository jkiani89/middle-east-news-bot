"""Phase T-1: three scheduled briefings per day, sectioned by topic."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

import pytest

from app.config import settings
from app.reports.daily_report import (
    current_slot_hour,
    format_slot_bulletin,
    group_items_by_topic,
    report_slot_hours,
    report_window,
    slot_label,
)
from app.scheduler.jobs import _publish_slot_briefing


def _item(event_id: int, topic_key: str | None, topic_fa: str, importance: int = 4) -> dict:
    return {
        "event_id": event_id,
        "topic_key": topic_key,
        "topic_fa": topic_fa,
        "importance": importance,
        "event_type": "MIL",
        "category_fa": "تحولات نظامی و امنیتی",
        "countries": ["ایران"],
        "actors": ["سپاه پاسداران"],
        "location": "تهران",
        "headline_fa": "تیتر خبری فارسی برای آزمون بولتن",
        "summary_fa": "خلاصه فارسی رویداد برای آزمون بخش‌بندی موضوعی بولتن.",
        "verification_status": "confirmed",
        "verification_status_fa": "✅ تأییدشده",
        "primary_source": "IRNA",
        "primary_url": "https://irna.ir/1",
        "additional_sources": [],
        "total_sources_count": 1,
    }


def test_slot_hours_come_from_settings():
    assert report_slot_hours() == (8, 20)
    assert slot_label(8) == "08:00"


def test_slot_windows_are_gapless_and_do_not_overlap():
    rep = date(2026, 11, 14)
    _, start8, end8 = report_window(rep, 8)
    _, start20, end20 = report_window(rep, 20)

    # Two twelve-hour bulletins: the morning one starts where last night's ended.
    assert end8 - start8 == timedelta(hours=12)
    assert end20 - start20 == timedelta(hours=12)
    assert start20 == end8
    assert end8.hour == 8 and end20.hour == 20


def test_legacy_window_is_unchanged_without_a_slot():
    _, start, end = report_window(date(2026, 11, 14))
    assert end - start == timedelta(days=1)
    assert end.hour == settings.daily_report_hour


def test_current_slot_hour_is_the_latest_due_slot():
    tz = ZoneInfo(settings.timezone)
    assert current_slot_hour(datetime(2026, 11, 14, 9, 15, tzinfo=tz)) == 8
    assert current_slot_hour(datetime(2026, 11, 14, 14, 5, tzinfo=tz)) == 8
    assert current_slot_hour(datetime(2026, 11, 14, 19, 59, tzinfo=tz)) == 8
    assert current_slot_hour(datetime(2026, 11, 14, 23, 0, tzinfo=tz)) == 20
    assert current_slot_hour(datetime(2026, 11, 14, 3, 0, tzinfo=tz)) == 20


def test_items_are_grouped_into_priority_ordered_sections():
    items = [
        _item(1, None, "سایر تحولات"),
        _item(2, "ye", "یمن و دریای سرخ"),
        _item(3, "ir", "ایران و مذاکرات"),
    ]
    sections = group_items_by_topic(items)

    assert [key for key, _name, _items in sections] == ["ir", "ye", "other"]
    assert [item["event_id"] for item in sections[0][2]] == [3]


def test_slot_bulletin_has_topic_sections_and_one_footer():
    sections = group_items_by_topic(
        [_item(1, "ir", "ایران و مذاکرات"), _item(2, "ye", "یمن و دریای سرخ")]
    )
    text = format_slot_bulletin(date(2026, 11, 14), sections, 20, {"min_importance": 2})

    assert "نوبت: 20:00" in text
    assert "ایران و مذاکرات" in text
    assert "یمن و دریای سرخ" in text
    assert text.count("یادداشت تحریریه") == 1
    assert "تیتر خبری فارسی" in text


class _FakeSession:
    def __init__(self, report: SimpleNamespace | None = None):
        self.report = report
        self.commits = 0

    async def scalar(self, stmt: object) -> object:
        return self.report

    async def commit(self) -> None:
        self.commits += 1


@pytest.mark.asyncio
async def test_already_published_slot_is_not_sent_again():
    report = SimpleNamespace(
        content={"bulletins": {"20:00": {"status": "published", "message_ids": [5]}}}
    )
    session = _FakeSession(report)

    with patch("app.scheduler.jobs.generate_slot_bulletins", new_callable=AsyncMock) as gen, \
         patch("app.scheduler.jobs.publish_report", new_callable=AsyncMock) as publish:
        gen.return_value = (date(2026, 11, 14), [], "text", {})

        sent = await _publish_slot_briefing(session, 20, None, dry_run=False)

    assert sent == 0
    assert publish.await_count == 0


@pytest.mark.asyncio
async def test_empty_slot_publishes_nothing():
    session = _FakeSession(SimpleNamespace(content={"bulletins": {}}))

    with patch("app.scheduler.jobs.generate_slot_bulletins", new_callable=AsyncMock) as gen, \
         patch("app.scheduler.jobs.publish_report", new_callable=AsyncMock) as publish:
        gen.return_value = (date(2026, 11, 14), [], "text", {"content_quality": "real"})

        sent = await _publish_slot_briefing(session, 8, None, dry_run=False)

    assert sent == 0
    assert publish.await_count == 0


@pytest.mark.asyncio
async def test_slot_briefing_publishes_text_and_pdf_and_records_ids():
    session = _FakeSession(
        SimpleNamespace(content={"bulletins": {"20:00": {"content_quality": "real"}}})
    )
    sections = [("ir", "ایران و مذاکرات", [_item(1, "ir", "ایران و مذاکرات")])]

    with patch("app.scheduler.jobs.generate_slot_bulletins", new_callable=AsyncMock) as gen, \
         patch("app.scheduler.jobs.publish_report", new_callable=AsyncMock) as publish, \
         patch("app.scheduler.jobs.publish_daily_report_pdf", new_callable=AsyncMock) as publish_pdf, \
         patch("app.scheduler.jobs.mark_slot_published", new_callable=AsyncMock) as mark:
        gen.return_value = (date(2026, 11, 14), sections, "text", {"content_quality": "real"})
        publish.return_value = [{"message_id": 21}, {"message_id": 22}]
        publish_pdf.return_value = {"message_id": 30, "type": "document"}

        sent = await _publish_slot_briefing(session, 20, None, dry_run=False)

    assert sent == 3
    assert publish.await_args.kwargs["persist"] is False
    mark.assert_awaited_once()
    ids = mark.await_args.args[3]
    assert ids == [21, 22, 30]
    assert mark.await_args.args[4] == 30


@pytest.mark.asyncio
async def test_dry_run_slot_never_marks_published():
    session = _FakeSession(SimpleNamespace(content={"bulletins": {}}))
    sections = [("ye", "یمن و دریای سرخ", [_item(2, "ye", "یمن و دریای سرخ")])]

    with patch("app.scheduler.jobs.generate_slot_bulletins", new_callable=AsyncMock) as gen, \
         patch("app.scheduler.jobs.publish_report", new_callable=AsyncMock) as publish, \
         patch("app.scheduler.jobs.publish_daily_report_pdf", new_callable=AsyncMock) as publish_pdf, \
         patch("app.scheduler.jobs.mark_slot_published", new_callable=AsyncMock) as mark:
        gen.return_value = (date(2026, 11, 14), sections, "text", {"content_quality": "real"})
        publish.return_value = [{"message_id": 1001}]

        sent = await _publish_slot_briefing(session, 8, None, dry_run=True)

    assert sent == 1
    assert publish_pdf.await_count == 0
    mark.assert_not_awaited()
