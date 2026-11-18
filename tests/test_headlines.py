"""Headline hygiene: a title that says nothing is replaced, visibly and predictably."""

from __future__ import annotations

import pytest

from app.editorial.headlines import (
    REASON_EMPTY,
    REASON_JUNK,
    REASON_OK,
    REASON_SOURCE_LABEL,
    REASON_TOO_SHORT,
    headline_rejection,
    sanitize_headline,
)
from app.reports.telegraph_report import build_item_block


def _item(**overrides) -> dict:
    item = {
        "event_id": 1,
        "headline_fa": "تیتر خبری معتبر",
        "summary_fa": "خلاصه‌ای که سامانه تولید کرده است.",
        "primary_source": "Reuters",
        "primary_url": "https://example.com/news/1",
        "first_seen_at": "2026-11-17T19:30:00+00:00",
        "verification_status_fa": "✅ دو منبع مستقل",
    }
    item.update(overrides)
    return item


@pytest.mark.parametrize(
    "raw",
    ["- رادیو فردا", "رادیو فردا", "| الجزیره |", "Radio Farda", "  رادیو فردا  -  "],
)
def test_a_bare_outlet_name_is_not_a_headline(raw):
    assert headline_rejection(raw, "Radio Farda") == REASON_SOURCE_LABEL


def test_a_real_headline_is_kept_verbatim():
    resolved = sanitize_headline("آتش‌بس در غزه پس از دو هفته", source="Reuters")

    assert resolved.reason == REASON_OK
    assert resolved.text == "آتش‌بس در غزه پس از دو هفته"
    assert resolved.replaced is False


def test_an_empty_or_placeholder_title_is_rejected():
    assert headline_rejection("", "Reuters") == REASON_EMPTY
    assert headline_rejection("   ", "Reuters") == REASON_EMPTY
    assert headline_rejection("خبر بدون عنوان", "Reuters") == REASON_JUNK


def test_a_single_word_is_not_a_headline():
    assert headline_rejection("توافق", "Reuters") == REASON_TOO_SHORT


def test_the_event_title_is_the_first_replacement():
    resolved = sanitize_headline(
        "- رادیو فردا",
        source="Radio Farda",
        alt="توافق آتش‌بس میان دو طرف در غزه",
        summary="نخستین دور مذاکرات پایان یافت.",
    )

    assert resolved.reason == REASON_SOURCE_LABEL
    assert resolved.text == "توافق آتش‌بس میان دو طرف در غزه"


def test_the_summary_lead_is_used_when_no_alternative_title_exists():
    resolved = sanitize_headline(
        "",
        source="Reuters",
        summary="نخستین دور مذاکرات پایان یافت. جزئیات بیشتر در ادامه می‌آید.",
    )

    assert resolved.text == "نخستین دور مذاکرات پایان یافت"


def test_a_source_and_time_label_is_the_last_resort():
    resolved = sanitize_headline("- رادیو فردا", source="Radio Farda", local_time="2026-11-17 20:00")

    assert resolved.reason == REASON_SOURCE_LABEL
    assert resolved.text == "خبر Radio Farda — 2026-11-17 20:00"


def test_without_any_material_the_generic_fallback_is_used():
    assert sanitize_headline("", source=None).text == "خبر بدون عنوان"


def test_rendering_counts_replaced_headlines_instead_of_hiding_them():
    stats: dict = {"headline_fallbacks": 0}
    item = _item(headline_fa="- رادیو فردا", headline_alt_fa="", summary_fa="خلاصه کوتاه خبر.")

    nodes = build_item_block(item, stats)

    headlines = [node["children"][0] for node in nodes if node.get("tag") == "h4"]
    assert headlines == ["خلاصه کوتاه خبر"]
    assert stats["headline_fallbacks"] == 1
    assert stats["headline_reasons"] == {REASON_SOURCE_LABEL: 1}


def test_a_valid_headline_does_not_touch_the_counter():
    stats: dict = {"headline_fallbacks": 0}

    build_item_block(_item(), stats)

    assert stats["headline_fallbacks"] == 0
    assert "headline_reasons" not in stats
