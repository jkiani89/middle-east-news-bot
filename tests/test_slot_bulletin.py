"""Scheduled 12-hour briefings: windows, sections, and publication state.

The bulletin itself is covered by the window/format tests; the delivery tests exercise
the two independent channels: Telegraph pages are created once, and a failed Telegram
notification is retried without rebuilding them.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
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
    slot_report_date,
)
from app.scheduler.jobs import publish_slot_briefing


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

    assert end8 - start8 == timedelta(hours=12)
    assert end20 - start20 == timedelta(hours=12)
    assert start20 == end8
    assert end8.hour == 8 and end20.hour == 20


def test_evening_window_is_the_exact_12_hours_before_it():
    tz = ZoneInfo(settings.timezone)
    _, start, end = report_window(date(2026, 11, 14), 20)
    assert start == datetime(2026, 11, 14, 8, 0, tzinfo=tz)
    assert end == datetime(2026, 11, 14, 20, 0, tzinfo=tz)


def test_morning_window_spans_the_previous_evening():
    tz = ZoneInfo(settings.timezone)
    _, start, end = report_window(date(2026, 11, 14), 8)
    assert start == datetime(2026, 11, 13, 20, 0, tzinfo=tz)
    assert end == datetime(2026, 11, 14, 8, 0, tzinfo=tz)


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


def test_late_run_keeps_the_slot_of_the_day_it_started():
    tz = ZoneInfo(settings.timezone)
    assert slot_report_date(20, datetime(2026, 11, 16, 0, 42, tzinfo=tz)) == date(2026, 11, 15)


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
    def __init__(self):
        self.commits = 0

    async def scalar(self, stmt: object) -> object:
        return None

    async def commit(self) -> None:
        self.commits += 1


@pytest.fixture(autouse=True)
def slot_table(monkeypatch):
    """In-memory stand-in for bulletin_slots and telegraph_reports.

    The real claims are single guarded SQL statements covered against PostgreSQL; these
    unit tests only need the same decisions plus the recorded channel states.
    """
    rows: dict = {}

    async def _slot_status(session, report_date, slot_hour):
        return rows.get((report_date, slot_hour))

    async def _claim_slot(session, report_date, slot_hour):
        key = (report_date, slot_hour)
        row = rows.get(key)
        if row and row["status"] == "published":
            return "published"
        if row and row["status"] == "in_flight":
            return "in_flight"
        rows[key] = {
            "status": "in_flight",
            "claimed_at": datetime.now(UTC),
            "message_ids": [],
            "telegram_status": "pending",
        }
        return "claimed"

    async def _mark_slot_published(session, report_date, slot_hour, message_ids):
        rows.setdefault((report_date, slot_hour), {})
        rows[(report_date, slot_hour)].update(
            {"status": "published", "message_ids": list(message_ids), "telegram_status": "sent"}
        )

    async def _release_slot(session, report_date, slot_hour):
        row = rows.get((report_date, slot_hour))
        if row is not None and row.get("status") == "in_flight":
            row["status"] = "pending"

    async def _record_telegram_status(session, report_date, slot_hour, status, message_id=None, error=None):
        rows.setdefault((report_date, slot_hour), {})
        rows[(report_date, slot_hour)]["telegram_status"] = status

    async def _mark_slot_telegraph(session, report_date, slot_hour, index_url, telegraph_status):
        rows.setdefault((report_date, slot_hour), {})
        rows[(report_date, slot_hour)]["telegraph_index_url"] = index_url
        rows[(report_date, slot_hour)]["telegraph_status"] = telegraph_status

    monkeypatch.setattr("app.scheduler.jobs.slot_status", _slot_status)
    monkeypatch.setattr("app.scheduler.jobs.claim_slot", _claim_slot)
    monkeypatch.setattr("app.scheduler.jobs.mark_slot_published", _mark_slot_published)
    monkeypatch.setattr("app.scheduler.jobs.release_slot", _release_slot)
    monkeypatch.setattr("app.scheduler.jobs.record_telegram_status", _record_telegram_status)
    monkeypatch.setattr("app.scheduler.jobs.mark_slot_telegraph", _mark_slot_telegraph)
    return rows


def _sections(items: list[dict]) -> list[tuple[str, str, list[dict]]]:
    return group_items_by_topic(items)


def _telegraph_result(status: str = "telegraph_completed", url: str = "https://telegra.ph/report-1"):
    return {
        "status": status,
        "index_url": url,
        "index_path": "report-1",
        "pages": {},
        "created": 0,
        "reused": 1,
        "failures": [],
        "skipped": status == "telegraph_completed",
        "item_count": 1,
        "window_start": None,
        "window_end": None,
    }


@pytest.mark.asyncio
async def test_already_published_slot_is_not_sent_again(slot_table):
    slot_table[(date(2026, 11, 14), 20)] = {
        "status": "published",
        "claimed_at": None,
        "message_ids": [5],
        "telegram_status": "sent",
    }

    with patch("app.scheduler.jobs.generate_slot_bulletins", new_callable=AsyncMock) as gen, \
         patch("app.scheduler.jobs.publish_telegraph_report", new_callable=AsyncMock) as telegraph, \
         patch("app.scheduler.jobs.publish_report", new_callable=AsyncMock) as publish:
        outcome = await publish_slot_briefing(
            _FakeSession(), 20, None, dry_run=False, report_date=date(2026, 11, 14)
        )

    assert outcome["messages"] == 0
    assert outcome["skipped"] == "published"
    assert gen.await_count == 0
    assert telegraph.await_count == 0
    assert publish.await_count == 0


@pytest.mark.asyncio
async def test_in_flight_slot_is_skipped_while_the_claim_is_fresh(slot_table):
    slot_table[(date(2026, 11, 14), 8)] = {
        "status": "in_flight",
        "claimed_at": datetime.now(UTC) - timedelta(minutes=2),
        "message_ids": [],
        "telegram_status": "pending",
    }

    with patch("app.scheduler.jobs.generate_slot_bulletins", new_callable=AsyncMock) as gen, \
         patch("app.scheduler.jobs.publish_report", new_callable=AsyncMock) as publish:
        outcome = await publish_slot_briefing(
            _FakeSession(), 8, None, dry_run=False, report_date=date(2026, 11, 14)
        )

    assert outcome["skipped"] == "in_flight"
    assert gen.await_count == 0
    assert publish.await_count == 0


@pytest.mark.asyncio
async def test_slot_is_claimed_before_the_first_send(slot_table):
    claim_at_send_time: dict = {}

    async def fake_telegraph(*args, **kwargs):
        claim_at_send_time.update(slot_table[(date(2026, 11, 14), 8)])
        return _telegraph_result()

    with patch("app.scheduler.jobs.generate_slot_bulletins", new_callable=AsyncMock) as gen, \
         patch("app.scheduler.jobs.publish_telegraph_report", side_effect=fake_telegraph), \
         patch("app.scheduler.jobs.publish_report", new_callable=AsyncMock) as publish:
        gen.return_value = (date(2026, 11, 14), _sections([_item(1, "ir", "ایران و مذاکرات")]), "text", {"content_quality": "real"})
        publish.return_value = [{"message_id": 51}]

        await publish_slot_briefing(
            _FakeSession(), 8, None, dry_run=False, report_date=date(2026, 11, 14)
        )

    assert claim_at_send_time.get("status") == "in_flight"
    assert claim_at_send_time.get("claimed_at")


@pytest.mark.asyncio
async def test_completed_telegraph_report_sends_the_notification_and_records_ids(slot_table):
    with patch("app.scheduler.jobs.generate_slot_bulletins", new_callable=AsyncMock) as gen, \
         patch("app.scheduler.jobs.publish_telegraph_report", new_callable=AsyncMock) as telegraph, \
         patch("app.scheduler.jobs.publish_report", new_callable=AsyncMock) as publish:
        gen.return_value = (date(2026, 11, 14), _sections([_item(1, "ir", "ایران و مذاکرات")]), "text", {"content_quality": "real"})
        telegraph.return_value = _telegraph_result()
        publish.return_value = [{"message_id": 21}, {"message_id": 22}]

        outcome = await publish_slot_briefing(
            _FakeSession(), 20, None, dry_run=False, report_date=date(2026, 11, 14)
        )

    assert outcome["messages"] == 2
    assert outcome["telegram_status"] == "sent"
    # The Telegraph path fetches the complete window itself (including stories with no
    # summary), instead of reusing the filtered Telegram bulletin items.
    assert telegraph.await_args.kwargs.get("items") is None
    row = slot_table[(date(2026, 11, 14), 20)]
    assert row["status"] == "published"
    assert row["message_ids"] == [21, 22]
    assert "telegra.ph/report-1" in publish.await_args.kwargs["report_text"]


@pytest.mark.asyncio
async def test_telegraph_failure_does_not_send_a_notification(slot_table):
    with patch("app.scheduler.jobs.generate_slot_bulletins", new_callable=AsyncMock) as gen, \
         patch("app.scheduler.jobs.publish_telegraph_report", new_callable=AsyncMock) as telegraph, \
         patch("app.scheduler.jobs.publish_report", new_callable=AsyncMock) as publish:
        gen.return_value = (date(2026, 11, 14), _sections([_item(1, "ir", "ایران و مذاکرات")]), "text", {"content_quality": "real"})
        telegraph.return_value = _telegraph_result(status="failed", url="")

        outcome = await publish_slot_briefing(
            _FakeSession(), 8, None, dry_run=False, report_date=date(2026, 11, 14)
        )

    assert outcome["telegram_status"] == "failed"
    assert publish.await_count == 0
    row = slot_table[(date(2026, 11, 14), 8)]
    assert row["status"] == "pending"  # released for the retry
    assert row["telegram_status"] == "failed"


@pytest.mark.asyncio
async def test_failed_notification_is_retried_without_rebuilding_pages(slot_table):
    """The retry run reuses the completed pages and only repeats the Telegram send."""
    slot_table[(date(2026, 11, 14), 8)] = {
        "status": "pending",
        "claimed_at": None,
        "message_ids": [],
        "telegram_status": "failed",
    }

    with patch("app.scheduler.jobs.generate_slot_bulletins", new_callable=AsyncMock) as gen, \
         patch("app.scheduler.jobs.publish_telegraph_report", new_callable=AsyncMock) as telegraph, \
         patch("app.scheduler.jobs.publish_report", new_callable=AsyncMock) as publish:
        gen.return_value = (date(2026, 11, 14), _sections([_item(1, "ir", "ایران و مذاکرات")]), "text", {"content_quality": "real"})
        telegraph.return_value = _telegraph_result()
        publish.return_value = [{"message_id": 77}]

        outcome = await publish_slot_briefing(
            _FakeSession(), 8, None, dry_run=False, report_date=date(2026, 11, 14)
        )

    assert outcome["telegram_status"] == "sent"
    assert telegraph.await_count == 1  # called, but the report module reuses its pages
    assert publish.await_count == 1
    assert slot_table[(date(2026, 11, 14), 8)]["message_ids"] == [77]


@pytest.mark.asyncio
async def test_empty_slot_publishes_nothing_but_is_recorded(slot_table):
    with patch("app.scheduler.jobs.generate_slot_bulletins", new_callable=AsyncMock) as gen, \
         patch("app.scheduler.jobs.publish_telegraph_report", new_callable=AsyncMock) as telegraph, \
         patch("app.scheduler.jobs.publish_report", new_callable=AsyncMock) as publish:
        gen.return_value = (date(2026, 11, 14), [], "text", {"content_quality": "real"})

        outcome = await publish_slot_briefing(
            _FakeSession(), 8, None, dry_run=False, report_date=date(2026, 11, 14)
        )

    assert outcome["messages"] == 0
    assert outcome["skipped"] == "empty"
    assert telegraph.await_count == 0
    assert publish.await_count == 0
    assert slot_table[(date(2026, 11, 14), 8)]["status"] == "published"


@pytest.mark.asyncio
async def test_dry_run_slot_never_marks_published(slot_table, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with patch("app.scheduler.jobs.generate_slot_bulletins", new_callable=AsyncMock) as gen, \
         patch("app.scheduler.jobs.publish_telegraph_report", new_callable=AsyncMock) as telegraph, \
         patch("app.scheduler.jobs.publish_report", new_callable=AsyncMock) as publish:
        gen.return_value = (date(2026, 11, 14), _sections([_item(2, "ye", "یمن و دریای سرخ")]), "text", {"content_quality": "real"})
        telegraph.return_value = _telegraph_result()
        publish.return_value = [{"message_id": 1001}]

        outcome = await publish_slot_briefing(
            _FakeSession(), 8, None, dry_run=True, report_date=date(2026, 11, 14)
        )

    assert outcome["messages"] == 1
    assert (date(2026, 11, 14), 8) not in slot_table
    assert publish.await_count == 1


@pytest.mark.asyncio
async def test_telegraph_disabled_falls_back_to_the_text_bulletin(slot_table):
    with patch("app.scheduler.jobs.generate_slot_bulletins", new_callable=AsyncMock) as gen, \
         patch("app.scheduler.jobs.publish_telegraph_report", new_callable=AsyncMock) as telegraph, \
         patch("app.scheduler.jobs.publish_report", new_callable=AsyncMock) as publish:
        gen.return_value = (date(2026, 11, 14), _sections([_item(1, "ir", "ایران و مذاکرات")]), "FULL TEXT", {"content_quality": "real"})
        telegraph.return_value = _telegraph_result(status="disabled", url="")
        publish.return_value = [{"message_id": 9}]

        outcome = await publish_slot_briefing(
            _FakeSession(), 8, None, dry_run=False, report_date=date(2026, 11, 14)
        )

    assert outcome["telegram_status"] == "sent"
    assert publish.await_args.kwargs["report_text"] == "FULL TEXT"
    assert slot_table[(date(2026, 11, 14), 8)]["status"] == "published"
