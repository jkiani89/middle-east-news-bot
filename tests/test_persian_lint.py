"""Phase Y-8: log-only Persian watchlist (``ai_payload['persian_lint']``)."""

from __future__ import annotations

from datetime import UTC, datetime

from app.ai.persian_lint import SUSPECT_WORDS, lint_persian_output
from app.ai.summarizer import EDITORIAL_SYSTEM_PROMPT, build_analysis_prompt
from app.editorial.glossary import canonicalize_approved_names


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


def test_phase_c_arabic_vocabulary_is_rewritten() -> None:
    """Phase C: the observed Arabic forms have a deterministic Persian replacement."""
    assert canonicalize_approved_names("طهران به واشنطن پیام فرستاد") == (
        "تهران به واشنگتن پیام فرستاد"
    )
    assert canonicalize_approved_names("مجزره کمین العتیبه و تصدید فشارها") == (
        "کشتار کمین العتیبه و تشدید فشارها"
    )
    assert canonicalize_approved_names("حراس شهرک و بحریه و استعوای سیاسی") == (
        "نگهبانان شهرک و نیروی دریایی و استعماری سیاسی"
    )
    assert canonicalize_approved_names("مسسیر ملی و اکشافات روشنگر") == (
        "مسیر ملی و اکتشافات روشنگر"
    )


def test_phase_c_leaves_legitimate_persian_words_alone() -> None:
    """«حراست» contains «حراس» but is a Persian word and must not be rewritten."""
    assert canonicalize_approved_names("حراست از تاسیسات هسته‌ای") == (
        "حراست از تاسیسات هسته‌ای"
    )
    assert canonicalize_approved_names("کشتار غیرنظامیان") == "کشتار غیرنظامیان"


def test_phase_c_rewrite_is_idempotent() -> None:
    once = canonicalize_approved_names("طهران: مجزره و تصدید")
    assert canonicalize_approved_names(once) == once


def test_phase_c_watchlist_records_the_new_forms() -> None:
    report = lint_persian_output("طهران", "مجزره و تصدید فشارها")
    words = {item["word"] for item in report["findings"]}
    assert {"طهران", "مجزره", "تصدید"} <= words
    assert report["blocking"] is False


def test_phase_c_prompt_states_the_standard_persian_rule() -> None:
    assert "فارسی معیار خبری" in EDITORIAL_SYSTEM_PROMPT
    assert "واشنطن" in EDITORIAL_SYSTEM_PROMPT


def test_phase_d_prompt_carries_the_date_and_the_title_rule() -> None:
    prompt = build_analysis_prompt("عنوان", "متن", "Reuters")
    today = datetime.now(UTC).date().isoformat()
    assert f"تاریخ امروز (UTC): {today}" in prompt
    assert "رئیس‌جمهور ایالات متحده آمریکا" in EDITORIAL_SYSTEM_PROMPT
    assert "رئیس‌جمهور پیشین" in EDITORIAL_SYSTEM_PROMPT


def test_phase_d_stale_trump_title_is_reported_but_never_blocks() -> None:
    report = lint_persian_output(
        "ترامپ: توافق نزدیک است",
        "رئیس‌جمهور پیشین آمریکا گفت که توافق ممکن است.",
    )
    assert report["official_titles"] == [
        {"actor": "دونالد ترامپ", "phrase": "رئیس‌جمهور پیشین"}
    ]
    assert report["blocking"] is False


def test_phase_d_stale_phrase_without_trump_is_not_reported() -> None:
    report = lint_persian_output("گزارش", "رئیس‌جمهور پیشین برزیل سخن گفت.")
    assert report["official_titles"] == []


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


def test_prompt_carries_kurdistan_geography_and_arabic_translation_rules() -> None:
    assert "اقلیم کردستان" in EDITORIAL_SYSTEM_PROMPT
    assert "کردستان عراق" in EDITORIAL_SYSTEM_PROMPT
    assert "فارسی معیار رسانه‌ای" in EDITORIAL_SYSTEM_PROMPT
    assert "تحت‌اللفظی" in EDITORIAL_SYSTEM_PROMPT


def test_prompt_carries_arabic_context_entity_consistency_and_actor_separation_rules() -> None:
    """Rule 12 covers contextual Arabicisms, headline/summary source fidelity, and actor separation."""
    assert "متناسب با بافت جمله" in EDITORIAL_SYSTEM_PROMPT
    assert "یدک‌کش دریایی" in EDITORIAL_SYSTEM_PROMPT
    assert "«الهيئة العامة للإحصاء» ← «سازمان کل آمار»" in EDITORIAL_SYSTEM_PROMPT
    assert "سازمان کل آمار عربستان سعودی" not in EDITORIAL_SYSTEM_PROMPT
    assert "مجلس سنای تگزاس" in EDITORIAL_SYSTEM_PROMPT
    assert "headline_fa و summary_fa باید دقیقاً مطابق متن اصلی خبر باشند" in EDITORIAL_SYSTEM_PROMPT
    assert "چندشخصیتی" in EDITORIAL_SYSTEM_PROMPT
    assert "وابستگی‌های سازمانی و اظهارات هر شخص را به شخص دیگری نسبت نده" in EDITORIAL_SYSTEM_PROMPT


def test_build_analysis_prompt_injects_arabic_editorial_glossary_terms() -> None:
    """Prompt-time glossary injection supplies Persian equivalents for matched Arabic terms."""
    prompt = build_analysis_prompt(
        "هجوم على قاطرة بحرية",
        "أعلنت الهيئة العامة للإحصاء عن تضرر مستودع قرب موقع مهرجان في المنطقة دون مفاجآت أو مفاجأة.",
        "الجزيرة",
    )
    assert "یدک‌کش دریایی" in prompt
    assert "- سازمان کل آمار ←" in prompt
    assert "سازمان کل آمار عربستان سعودی" not in prompt
    assert "انبار" in prompt
    assert "جشنواره" in prompt
    assert "شگفتی‌ها" in prompt
    assert "شگفتی ← مفاجأة" in prompt

