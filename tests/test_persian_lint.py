"""Phase Y-8: log-only Persian watchlist (``ai_payload['persian_lint']``)."""

from __future__ import annotations

from app.ai.persian_lint import SUSPECT_WORDS, lint_persian_output


def test_a_watchlist_word_is_recorded_and_never_blocks() -> None:
    report = lint_persian_output(
        "نشت واژه‌های عربی در ترجمه",
        "در این گزارش «مسرح عملیات» و «مظله امنیتی» آمده و «تقویضی بر توافق» تکرار شده است.",
    )
    assert report["mode"] == "log"
    assert report["blocking"] is False
    assert {item["word"] for item in report["findings"]} == {"مسرح", "مظله", "تقویض"}
    assert all(item["count"] >= 1 for item in report["findings"])


def test_clean_persian_text_records_no_finding() -> None:
    report = lint_persian_output(
        "نشست منطقه‌ای در ژنو برگزار شد",
        "هیئت‌های منطقه‌ای درباره آتش‌بس گفت‌وگو کردند و بیانیه‌ای منتشر نشد.",
    )
    assert report["findings"] == []
    assert report["blocking"] is False
    assert report["watchlist"] == list(SUSPECT_WORDS)
