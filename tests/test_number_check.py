"""Phase Y-1: deterministic numeric grounding check for Persian output."""

from __future__ import annotations

import pytest

from app.ai.number_check import MODEL_BODY_CHAR_LIMIT, check_numbers, extract_numbers


def _check(headline: str, summary: str, source: str, *, title: str = "", mode: str = "log"):
    return check_numbers(
        headline=headline,
        summary=summary,
        source_title=title,
        source_body=source,
        mode=mode,
    )


# Acceptance Y-1.1
def test_number_absent_from_source_is_reported() -> None:
    report = _check(
        "دست‌کم ۱۲ نفر کشته شدند",
        "منابع محلی از کشته شدن ۱۲ نفر خبر دادند.",
        "Local sources reported casualties after the strike.",
    )
    assert report["status"] == "mismatch"
    assert report["blocking"] is True
    # The number appears in both the headline and the summary, so both are reported.
    assert report["unmatched"] == [
        {"value": "12", "reason_code": "not_in_source", "token": "12"},
        {"value": "12", "reason_code": "not_in_source", "token": "12"},
    ]
    assert report["reason_codes"]["not_in_source"] == 2


# Acceptance Y-1.2
@pytest.mark.parametrize("output_number", ["۱٬۲۰۰", "1,200", "١٢٠٠", "1200", "1 200"])
@pytest.mark.parametrize("source_number", ["۱٬۲۰۰", "1,200", "١٢٠٠", "1200", "1 200"])
def test_digit_and_separator_variants_are_equivalent(output_number: str, source_number: str) -> None:
    report = _check(
        f"{output_number} خودرو منهدم شد",
        "در این حمله شمار خودروهای منهدم‌شده اعلام شد.",
        f"The army said {source_number} vehicles were destroyed.",
    )
    assert report["status"] == "clean", report["unmatched"]
    assert report["blocking"] is False


# Acceptance Y-1.3
def test_year_and_date_present_in_source_are_not_flagged() -> None:
    report = _check(
        "نشست ۱۴ نوامبر ۲۰۲۶ در ژنو برگزار شد",
        "این نشست در تاریخ 2026-11-14 با حضور هیئت‌ها برگزار شد.",
        "The talks were held on 2026-11-14 in Geneva, the second round since 2024.",
    )
    assert report["status"] == "clean", report["unmatched"]


# Acceptance Y-1.6
def test_number_only_present_after_prompt_truncation_is_not_grounded() -> None:
    body = ("بدون عدد " * 3000)[:MODEL_BODY_CHAR_LIMIT] + " 12"
    assert len(body) > MODEL_BODY_CHAR_LIMIT
    report = _check("۱۲ نفر کشته شدند", "منابع از کشته شدن ۱۲ نفر خبر دادند.", body)
    assert report["checked_source_chars"] == MODEL_BODY_CHAR_LIMIT
    assert report["source_truncated"] is True
    assert report["status"] == "mismatch"
    assert report["blocking"] is True


def test_scale_conversion_between_word_and_number_is_matched() -> None:
    report = _check(
        "حدود ۳ میلیون نفر آواره شدند",
        "سازمان‌های امدادی از آوارگی ۳ میلیون نفر خبر دادند.",
        "Aid agencies said 3,000,000 people were displaced.",
    )
    assert report["status"] == "clean", report["unmatched"]
    assert report["reason_codes"].get("scale_converted") == 2


def test_scale_mismatch_is_labelled() -> None:
    report = _check(
        "۳ میلیون نفر آواره شدند",
        "گزارش‌ها از آوارگی گسترده خبر می‌دهند.",
        "Aid agencies said 300,000 people were displaced.",
    )
    assert report["reason_codes"]["scale_mismatch"] == 1
    assert report["blocking"] is True


def test_spelled_out_english_number_in_source_is_accepted() -> None:
    report = _check(
        "۳ کشته در حمله",
        "منابع محلی از کشته شدن ۳ نفر خبر دادند.",
        "Three people were killed in the attack, officials said.",
    )
    assert report["status"] == "clean", report["unmatched"]
    assert report["reason_codes"].get("word_number_source") == 2


def test_spelled_out_arabic_number_in_source_is_accepted() -> None:
    report = _check(
        "۳ کشته در حمله",
        "منابع از کشته شدن ۳ نفر خبر دادند.",
        "أعلنت السلطات مقتل ثلاثة أشخاص في الغارة.",
    )
    assert report["status"] == "clean", report["unmatched"]
    assert report["reason_codes"].get("word_number_source") == 2


def test_ambiguous_source_separator_is_counted_not_flagged() -> None:
    report = _check(
        "۱۲۰۰ خودرو منهدم شد",
        "شمار خودروهای منهدم‌شده 1200 اعلام شد.",
        "Officials put the toll at 1,200 vehicles.",
    )
    assert report["status"] == "clean", report["unmatched"]
    assert report["reason_codes"].get("ambiguous_separator", 0) >= 1


def test_jalali_year_is_reported_but_never_blocks() -> None:
    report = _check(
        "در سال ۱۴۰۴ این توافق امضا شد",
        "این توافق در سال ۱۴۰۴ نهایی شد.",
        "The agreement was signed in 2025, officials said.",
    )
    assert report["reason_codes"]["jalali_calendar"] == 2
    assert report["blocking"] is False


def test_off_mode_skips_the_check() -> None:
    report = _check("۱۲ نفر", "۱۲ نفر", "no numbers", mode="off")
    assert report["status"] == "skipped"
    assert report["unmatched"] == []
    assert report["blocking"] is False


def test_extract_numbers_exposes_both_readings_of_thousands_separator() -> None:
    values = {value for value, _, _ in extract_numbers("1,200 and 1,200,000")}
    assert {1200.0, 1.2, 1_200_000.0} <= values
