"""Dedup stability checks for Phase 1 (stable event_id + empty topic_keys fallback)."""

from __future__ import annotations

from app.editorial.telegraph_topics import (
    _item_topic_keys,
    group_items_by_telegraph_topic,
)
from app.reports.telegraph_report import build_index_page, format_admin_report


def _item(event_id=None, topic_key="ir", topic_keys=None, headline="خبر"):
    d = {
        "event_id": event_id,
        "topic_key": topic_key,
        "topic_fa": "ایران و آمریکا" if topic_key == "ir" else "سایر تحولات",
        "headline_fa": headline,
        "summary_fa": "خلاصه",
        "primary_source": "SRC",
        "primary_url": "https://example.com/x",
        "first_seen_at": "2026-11-14T07:30:00+00:00",
        "verification_status": "unconfirmed",
        "verification_status_fa": "❓",
        "additional_sources": [],
    }
    if topic_keys is not None:
        d["topic_keys"] = topic_keys
    return d


def test_two_independent_dicts_same_event_id_dedup_in_one_section():
    a = _item(event_id=42, topic_key="ir")
    b = _item(event_id=42, topic_key="ir", headline="خبر دیگر")
    sections = group_items_by_telegraph_topic([a, b])
    ir_items = [s for s in sections if s[0] == "ir"][0][2]
    assert len(ir_items) == 1


def test_two_items_different_event_ids_both_survive():
    a = _item(event_id=1, topic_key="ir")
    b = _item(event_id=2, topic_key="ir", headline="خبر دیگر")
    sections = group_items_by_telegraph_topic([a, b])
    ir_items = [s for s in sections if s[0] == "ir"][0][2]
    assert len(ir_items) == 2


def test_item_without_event_id_preserves_previous_behavior():
    a = _item(event_id=None, topic_key="ir")
    b = _item(event_id=None, topic_key="ir", headline="خبر دیگر")
    sections = group_items_by_telegraph_topic([a, b])
    ir_items = [s for s in sections if s[0] == "ir"][0][2]
    assert len(ir_items) == 2


def test_empty_topic_keys_with_topic_key_ir_goes_to_iran():
    item = _item(event_id=7, topic_key="ir", topic_keys=[])
    assert _item_topic_keys(item) == ["ir"]
    sections = group_items_by_telegraph_topic([item])
    assert any(s[0] == "ir" for s in sections)
    assert not any(s[0] == "other" for s in sections)


def test_empty_topic_keys_without_topic_key_goes_to_other():
    item = _item(event_id=8, topic_key=None, topic_keys=[])
    assert _item_topic_keys(item) == [None]
    sections = group_items_by_telegraph_topic([item])
    assert any(s[0] == "other" for s in sections)


def test_multi_topic_item_published_in_all_and_counted_once_in_index():
    item = _item(event_id=99, topic_key="ir", topic_keys=["ir", "il", "me"])
    sections = group_items_by_telegraph_topic([item])
    keys = [s[0] for s in sections]
    assert "ir" in keys and "il" in keys and "me" in keys
    # index unique count
    import json
    from datetime import date, datetime
    from zoneinfo import ZoneInfo

    import app.config as cfg

    tz = ZoneInfo(cfg.settings.timezone)
    start = datetime(2026, 11, 14, 8, 0, tzinfo=tz)
    end = datetime(2026, 11, 14, 8, 0, tzinfo=tz)
    topic_pages = {k: {"url": f"https://telegra.ph/{k}"} for k in keys}
    _title, nodes = build_index_page(date(2026, 11, 14), 8, sections, topic_pages, start, end, {})
    rendered = json.dumps(nodes, ensure_ascii=False)
    # unique count should be 1
    assert "تعداد اخبار این بازه: 1" in rendered

    text = format_admin_report(
        date(2026, 11, 14), 8, "https://telegra.ph/report-1", sections,
        {"window_start": "2026-11-13T20:00:00+00:00", "window_end": "2026-11-14T08:00:00+00:00"},
    )
    assert "1 خبر" in text.split("\n")[2] or "📰 1 خبر" in text
