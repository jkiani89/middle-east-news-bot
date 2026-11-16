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


def test_tahjir_is_in_watchlist_and_recorded() -> None:
    """Y-12 language polish: 'تهجیر' (Arabic displaced/displacement) is flagged in log."""
    assert "تهجیر" in SUSPECT_WORDS
    report = lint_persian_output(
        "گزارش عملیات در کرانه باختری",
        "این اقدام منجر به تهجیر هزاران فلسطینی شده است.",
    )
    assert report["mode"] == "log"
    assert report["blocking"] is False
    assert any(item["word"] == "تهجیر" and item["count"] == 1 for item in report["findings"])


def test_location_detection_headline_quote_narrative() -> None:
    """Verifies that the occurrence location (headline, quote, narrative) is accurately recorded."""
    # 1. Headline only
    rep1 = lint_persian_output("مسرح عملیات در شمال", "هیچ اتفاق خاصی نیفتاد.")
    f1 = next(item for item in rep1["findings"] if item["word"] == "مسرح")
    assert f1["locations"] == ["headline"]

    # 2. Quote only (inside Persian quotation marks «...»)
    rep2 = lint_persian_output("عملیات جدید", "سخنگوی ارتش گفت: «تهجیر ساکنان ادامه نخواهد داشت».")
    f2 = next(item for item in rep2["findings"] if item["word"] == "تهجیر")
    assert f2["locations"] == ["quote"]

    # 3. Narrative only (in summary outside quotes)
    rep3 = lint_persian_output("عملیات جدید", "منابع محلی از تهجیر اجباری اهالی منطقه خبر دادند.")
    f3 = next(item for item in rep3["findings"] if item["word"] == "تهجیر")
    assert f3["locations"] == ["narrative"]

    # 4. Both headline and quote
    rep4 = lint_persian_output("سیاست تهجیر در منطقه", "او تأکید کرد: «تهجیر خط قرمز ماست».")
    f4 = next(item for item in rep4["findings"] if item["word"] == "تهجیر")
    assert sorted(f4["locations"]) == ["headline", "quote"]
