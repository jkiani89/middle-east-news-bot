"""Phase Y-1: deterministic numeric grounding check for Persian output."""

from __future__ import annotations

import json
import time

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
        {"value": "12", "reason_code": "not_in_source", "token": "12", "dual_token": None},
        {"value": "12", "reason_code": "not_in_source", "token": "12", "dual_token": None},
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


# ── Y-4: full parsing before anything is labelled unverifiable ─────────────────


def test_hyphenated_spelled_number_is_parsed() -> None:
    report = _check(
        "۲۳ نفر کشته شدند",
        "منابع از کشته شدن ۲۳ نفر خبر دادند.",
        "Twenty-three people were killed, officials said.",
    )
    assert report["status"] == "clean", report["unmatched"]
    assert report["reason_codes"].get("word_number_source") == 2


def test_spelled_number_with_and_is_parsed() -> None:
    report = _check(
        "۲۳ نفر کشته شدند",
        "منابع از کشته شدن ۲۳ نفر خبر دادند.",
        "Twenty three people were killed.",
    )
    assert report["status"] == "clean", report["unmatched"]


def test_persian_spelled_number_is_parsed() -> None:
    report = _check(
        "۲۳ نفر کشته شدند",
        "منابع از کشته شدن ۲۳ نفر خبر دادند.",
        "منابع گفتند بیست و سه نفر کشته شدند.",
    )
    assert report["status"] == "clean", report["unmatched"]


def test_attached_billion_unit_is_parsed() -> None:
    report = _check(
        "۳ میلیارد دلار خسارت",
        "خسارت ۳ میلیارد دلار برآورد شد.",
        "Damage was estimated at 3bn dollars.",
    )
    assert report["status"] == "clean", report["unmatched"]
    assert report["reason_codes"].get("scale_converted") == 2


def test_attached_m_unit_keeps_both_readings() -> None:
    for output in ("۲٫۵ میلیون", "۲٫۵"):
        report = _check(
            output,
            "برآوردها منتشر شد.",
            "The fund is worth 2.5m dollars.",
        )
        assert report["status"] == "clean", (output, report["unmatched"])


def test_arabic_inflected_scale_word_is_parsed() -> None:
    report = _check(
        "۳ میلیون آواره",
        "گزارش‌ها از آوارگی گسترده خبر می‌دهند.",
        "أعلنت الأمم المتحدة نزوح 3 مليونًا من المدنيين.",
    )
    assert report["status"] == "clean", report["unmatched"]


def test_unparsed_unit_only_makes_its_own_number_unverifiable() -> None:
    report = _check(
        "۳۰۰ هزار بشکه و ۷۷ کشته",
        "جزئیات بیشتری منتشر نشد.",
        "Output reached 3 lakh barrels, while 3bn dollars were pledged.",
    )
    codes = [item["reason_code"] for item in report["unmatched"]]
    assert "unverifiable" in codes
    assert codes.count("not_in_source") == 1
    assert report["blocking"] is True


def test_fabricated_number_next_to_parsed_unit_still_blocks() -> None:
    report = _check(
        "۷۷ نفر کشته شدند",
        "منابع از کشته شدن ۷۷ نفر خبر دادند.",
        "Damage was estimated at 3bn dollars.",
    )
    assert report["blocking"] is True
    assert report["reason_codes"]["not_in_source"] == 2


def test_unverifiable_never_blocks() -> None:
    report = _check(
        "۳۰۰ هزار بشکه صادر شد",
        "صادرات ادامه دارد.",
        "Output reached 3 lakh barrels.",
    )
    assert report["reason_codes"]["unverifiable"] == 1
    assert report["blocking"] is False


def test_arabic_spelled_compound_is_summed() -> None:
    """Y-5 changed this: «ثلاثة وعشرون» is now summed instead of unverifiable."""
    report = _check(
        "۲۳ نفر کشته شدند",
        "منابع از کشته شدن ۲۳ نفر خبر دادند.",
        "أعلنت السلطات مقتل ثلاثة وعشرون شخصا.",
    )
    assert report["status"] == "clean", report["unmatched"]
    assert report["reason_codes"].get("word_number_source") == 2
    assert report["blocking"] is False


# ── Y-1c: adversarial input must stay linear ───────────────────────────────


@pytest.mark.parametrize(
    "text",
    [
        "1,234,567 " * 1500,
        "twenty-three " * 1000,
        "3bn 2.5m 5k مليونًا " * 700,
        "12,34.56-78 9,876 " * 800,
    ],
    ids=["digits-commas", "hyphens", "mixed-scale", "punctuation"],
)
def test_pathological_input_finishes_within_budget(text: str) -> None:
    body = (text * 2)[:MODEL_BODY_CHAR_LIMIT]
    assert len(body) == MODEL_BODY_CHAR_LIMIT
    start = time.perf_counter()
    report = check_numbers(
        headline=body[:200],
        summary=body[200:600],
        source_title="t",
        source_body=body,
        mode="log",
    )
    elapsed = time.perf_counter() - start

    assert report["checked_source_chars"] == MODEL_BODY_CHAR_LIMIT
    assert report["numbers_checked"] > 0
    assert report["blocking"] in (True, False)
    json.dumps(report)  # must stay JSON-serialisable for the payload
    assert elapsed < 2.0, f"check took {elapsed:.2f}s on {len(body)} chars"


def test_off_mode_skips_the_check() -> None:
    report = _check("۱۲ نفر", "۱۲ نفر", "no numbers", mode="off")
    assert report["status"] == "skipped"
    assert report["unmatched"] == []
    assert report["blocking"] is False


def test_extract_numbers_exposes_both_readings_of_thousands_separator() -> None:
    values = {value for value, _, _ in extract_numbers("1,200 and 1,200,000")}
    assert {1200.0, 1.2, 1_200_000.0} <= values


# ── Y-5: ambiguous words are only numbers in a numeric phrase ──────────────────


def test_bare_ambiguous_word_is_not_a_number() -> None:
    report = _check(
        "یک نفر در حمله زخمی شد",
        "منابع محلی از زخمی شدن یک نفر خبر دادند.",
        "Local sources said one person was wounded in the attack.",
    )
    assert report["numbers_checked"] == 0, report["unmatched"]
    assert report["status"] == "clean"


def test_ambiguous_word_before_a_scale_word_is_a_number() -> None:
    report = _check(
        "یک میلیون نفر آواره شدند",
        "گزارش‌ها از آوارگی گسترده خبر می‌دهند.",
        "Aid agencies said 1,000,000 people were displaced.",
    )
    assert report["status"] == "clean", report["unmatched"]
    assert report["numbers_checked"] == 1
    assert report["matched"] == 1


@pytest.mark.parametrize("word", ["نه", "واحد"])
def test_bare_no_and_unit_words_are_ignored(word: str) -> None:
    report = _check(
        f"{word} مورد گزارش شد",
        "جزئیات بیشتری منتشر نشد.",
        "No further details were released.",
    )
    assert report["numbers_checked"] == 0, report["unmatched"]


def test_bare_scale_word_is_ignored() -> None:
    report = _check(
        "میلیارد در بودجه گم شد",
        "جزئیات بیشتری منتشر نشد.",
        "No further details were released.",
    )
    assert report["numbers_checked"] == 0, report["unmatched"]


# ── Y-5: Arabic spelled-out numbers in the source ─────────────────────────────


def test_arabic_unit_word_in_source_matches_digit_output() -> None:
    report = _check(
        "۳ کشته در حمله",
        "منابع از کشته شدن ۳ نفر خبر دادند.",
        "أعلنت السلطات مقتل ثلاثة أشخاص في الغارة.",
    )
    assert report["status"] == "clean", report["unmatched"]
    assert report["reason_codes"].get("word_number_source") == 2


def test_arabic_teen_compound_is_parsed() -> None:
    assert {value for value, _, _ in extract_numbers("خمسة عشر")} == {15.0}
    report = _check(
        "۱۵ کشته در حمله",
        "منابع از کشته شدن ۱۵ نفر خبر دادند.",
        "أعلنت السلطات مقتل خمسة عشر شخصا.",
    )
    assert report["status"] == "clean", report["unmatched"]


def test_arabic_hundred_and_tens_compound_is_parsed() -> None:
    assert {value for value, _, _ in extract_numbers("مئة وثلاثين")} == {130.0}
    report = _check(
        "۱۳۰ کشته در حمله",
        "منابع از کشته شدن ۱۳۰ نفر خبر دادند.",
        "أعلنت السلطات مقتل مئة وثلاثين شخصا.",
    )
    assert report["status"] == "clean", report["unmatched"]


def test_digit_output_without_source_number_still_blocks() -> None:
    report = _check(
        "۱۳۰ نفر کشته شدند",
        "جزئیات بیشتری منتشر نشد.",
        "Officials said many people were killed.",
    )
    assert [item["reason_code"] for item in report["unmatched"]] == ["not_in_source"]
    assert report["blocking"] is True
    assert report["status"] == "mismatch"


# ── Y-5: status separates a real mismatch from an informational finding ───────


def test_jalali_only_article_is_unverifiable_not_mismatch() -> None:
    report = _check(
        "در سال ۱۴۰۴ این توافق امضا شد",
        "این توافق در سال ۱۴۰۴ نهایی شد.",
        "The agreement was signed in 2025, officials said.",
    )
    assert report["reason_codes"]["jalali_calendar"] == 2
    assert report["status"] == "unverifiable"
    assert report["status"] != "mismatch"
    assert report["blocking"] is False


def test_number_check_stays_local_and_needs_no_model_call(monkeypatch: pytest.MonkeyPatch) -> None:
    import socket

    def _no_network(*args: object, **kwargs: object) -> None:
        raise AssertionError("the number check must not make a model or network call")

    monkeypatch.setattr(socket, "socket", _no_network)
    report = _check(
        "۳ میلیون نفر آواره شدند",
        "سازمان‌های امدادی از آوارگی گسترده خبر دادند.",
        "Aid agencies said 3,000,000 people were displaced.",
    )
    assert report["status"] == "clean", report["unmatched"]


# ── Y-5 review: no text-wide fuzzy fallback ───────────────────────────────────


def test_parsable_arabic_source_does_not_excuse_a_different_output_number() -> None:
    report = _check(
        "۸ نفر کشته شدند",
        "جزئیات بیشتری منتشر نشد.",
        "أعلنت السلطات مقتل ثلاثة أشخاص.",
    )
    assert [item["reason_code"] for item in report["unmatched"]] == ["not_in_source"]
    assert report["unmatched"][0]["value"] == "8"
    assert report["blocking"] is True
    assert report["status"] == "mismatch"


def test_dual_arabic_source_does_not_excuse_a_different_output_number() -> None:
    report = _check(
        "۱۲ نفر کشته شدند",
        "جزئیات بیشتری منتشر نشد.",
        "مقتل اثنين من المدنيين.",
    )
    assert [item["reason_code"] for item in report["unmatched"]] == ["not_in_source"]
    assert report["blocking"] is True


def test_unparsed_arabic_token_cannot_excuse_an_unrelated_number() -> None:
    """«عشرات» is not a figure, and must not turn 8 into a non-blocking finding."""
    report = _check(
        "۸ نفر کشته شدند",
        "جزئیات بیشتری منتشر نشد.",
        "شهدت المنطقة عشرات الغارات خلال الأسبوع الماضي.",
    )
    assert [item["reason_code"] for item in report["unmatched"]] == ["not_in_source"]
    assert report["blocking"] is True
    assert report["status"] == "mismatch"


def test_arabic_definite_article_is_parsed_not_guessed() -> None:
    assert {value for value, _, _ in extract_numbers("العشرين")} == {20.0}
    assert {value for value, _, _ in extract_numbers("المئة")} == {100.0}


# ── Y-5 review: Arabic corpora must stay linear ──────────────────────────────

_ARABIC_FILLER = (
    "أعلنت وزارة الخارجية أن المحادثات استمرت في العاصمة وسط إجراءات أمنية مشددة",
    "وقال مسؤول محلي إن الفرق الطبية نقلت المصابين إلى المستشفيات القريبة",
    "وأشار بيان صادر عن الأمم المتحدة إلى أن الوضع الإنساني تدهور خلال الأسبوع الماضي",
    "من جهته أكد المتحدث باسم الحكومة أن اللجان الفنية واصلت اجتماعاتها في مقر الوزارة",
)
_ARABIC_NUMERIC_WORDS = (
    "خمسة عشر",
    "مئة وثلاثين",
    "ثلاثة وعشرون",
    "خمسة وعشرون",
    "مئة",
    "ثلاثة",
)


def _arabic_news_corpus() -> tuple[str, int]:
    """~2500 words of Arabic news copy with ~50 spelled-out numbers scattered in."""
    sentences: list[str] = []
    numeric = 0
    index = 0
    while len(" ".join(sentences)) < MODEL_BODY_CHAR_LIMIT:
        sentence = _ARABIC_FILLER[index % len(_ARABIC_FILLER)]
        if index % 4 == 0:
            sentence = f"{_ARABIC_NUMERIC_WORDS[index % len(_ARABIC_NUMERIC_WORDS)]} {sentence}"
            numeric += 1
        sentences.append(sentence)
        index += 1
    return " ".join(sentences)[:MODEL_BODY_CHAR_LIMIT], numeric


def _arabic_adversarial_corpus() -> str:
    """Arabic number words back to back, with the glued conjunction «و»."""
    return ("خمسة عشر مئة وثلاثين ثلاثة وعشرون مئة ألف " * 600)[:MODEL_BODY_CHAR_LIMIT]


def test_arabic_news_corpus_finishes_within_budget() -> None:
    body, numeric = _arabic_news_corpus()
    assert len(body) == MODEL_BODY_CHAR_LIMIT
    assert 2_000 <= len(body.split()) <= 3_200
    assert 40 <= numeric <= 60

    start = time.perf_counter()
    report = check_numbers(
        headline=body[:200],
        summary=body[200:600],
        source_title="t",
        source_body=body,
        mode="log",
    )
    elapsed_ms = (time.perf_counter() - start) * 1000

    assert report["checked_source_chars"] == MODEL_BODY_CHAR_LIMIT
    json.dumps(report)
    assert elapsed_ms < 1_000, f"check took {elapsed_ms:.0f}ms on Arabic news corpus"


def test_arabic_adversarial_corpus_finishes_within_budget() -> None:
    body = _arabic_adversarial_corpus()
    assert len(body) == MODEL_BODY_CHAR_LIMIT

    start = time.perf_counter()
    report = check_numbers(
        headline=body[:200],
        summary=body[200:600],
        source_title="t",
        source_body=body,
        mode="log",
    )
    elapsed_ms = (time.perf_counter() - start) * 1000

    assert report["checked_source_chars"] == MODEL_BODY_CHAR_LIMIT
    json.dumps(report)
    assert elapsed_ms < 1_000, f"check took {elapsed_ms:.0f}ms on back-to-back Arabic numbers"


# ── Y-6: remaining false positives ──────────────────────────────────────────

def test_arabic_digit_with_arabic_scale_word_matches() -> None:
    report = _check("۱۳۰ میلیارد دلار", "گزارش شد.", "130 مليار دولار")
    assert report["status"] == "clean"
    assert report["reason_codes"].get("scale_converted") == 1


def test_arabic_digit_with_arabic_scale_plural_matches() -> None:
    report = _check("۳ میلیون نفر", "گزارش شد.", "3 ملايين")
    assert report["status"] == "clean"


def test_arabic_digit_with_arabic_scale_mismatch_stays_blocking() -> None:
    report = _check("۱۳ میلیارد دلار", "گزارش شد.", "130 مليار")
    assert report["blocking"] is True
    assert report["unmatched"][0]["reason_code"] == "scale_mismatch"


def test_arabic_scale_word_tolerates_zwnj_and_tatweel() -> None:
    report = _check("۵ میلیون", "گزارش شد.", "5\u200cمليـون")
    assert report["status"] == "clean"


def test_arabic_dual_token_makes_two_unverifiable() -> None:
    report = _check("۲ نفر کشته شدند", "جزئیات بیشتری منتشر نشد.", "أعلنت مقتل شهيدان في Gaza")
    assert report["status"] == "unverifiable"
    assert report["blocking"] is False
    assert [item["reason_code"] for item in report["unmatched"]] == ["arabic_dual_possible"]
    assert report["unmatched"][0]["dual_token"] == "شهيدان"


# Y-6b: the trigger is a restricted noun list, not any dual-suffix word.
def test_dual_suffix_place_names_do_not_trigger() -> None:
    source = (
        "أعلنت إيران ولبنان عن مقتل مدنيين، ونقلت وسائل إعلام عن الفلسطينيين "
        "أن المدنيين يتعرضون للقصف."
    )
    report = _check("۲ نفر کشته شدند", "جزئیات بیشتری منتشر نشد.", source)
    assert report["status"] == "mismatch"
    assert report["blocking"] is True
    assert report["unmatched"][0]["reason_code"] == "not_in_source"
    assert report["unmatched"][0]["dual_token"] is None


def test_restricted_dual_noun_triggers_with_article() -> None:
    report = _check("۲ نفر کشته شدند", "جزئیات بیشتری منتشر نشد.", "أعلنت أن البلدين على حافة المواجهة")
    assert report["status"] == "unverifiable"
    assert report["blocking"] is False
    assert report["unmatched"][0]["reason_code"] == "arabic_dual_possible"
    assert report["unmatched"][0]["dual_token"] == "البلدين"


def test_dual_list_requires_value_two() -> None:
    report = _check("۳ مناطق", "گزارش شد.", "استهدف الجانبين في البلدين")
    assert report["blocking"] is True
    assert report["unmatched"][0]["reason_code"] == "not_in_source"


def test_arabic_dual_requires_value_two() -> None:
    report = _check("۳ نفر کشته شدند", "جزئیات بیشتری منتشر نشد.", "أعلنت مقتل فلسطينيان")
    assert report["blocking"] is True
    assert report["unmatched"][0]["reason_code"] == "not_in_source"


def test_arabic_dual_requires_value_two_twelve() -> None:
    report = _check("۱۲ نفر کشته شدند", "جزئیات بیشتری منتشر نشد.", "أعلنت مقتل شهيدان")
    assert report["blocking"] is True
    assert report["unmatched"][0]["reason_code"] == "not_in_source"


def test_arabic_dual_requires_arabic_source() -> None:
    report = _check("۲ نفر کشته شدند", "جزئیات بیشتری منتشر نشد.", "Officials reported casualties in the region.")
    assert report["blocking"] is True
    assert report["unmatched"][0]["reason_code"] == "not_in_source"


def test_arabic_dual_requires_dual_token() -> None:
    report = _check("۲ نفر کشته شدند", "جزئیات بیشتری منتشر نشد.", "أعلنت الوزارة عن الحادث")
    assert report["blocking"] is True
    assert report["unmatched"][0]["reason_code"] == "not_in_source"


def test_zwnj_compound_persian_word_is_not_a_number() -> None:
    numbers = [value for value, _, _ in extract_numbers("سه‌شنبه گذشته")]
    assert 3.0 not in numbers
    numbers = [value for value, _, _ in extract_numbers("دو‌جانبه بود")]
    assert 2.0 not in numbers
    numbers = [value for value, _, _ in extract_numbers("سه‌بعدی و دو‌جانبه")]
    assert 3.0 not in numbers
    assert 2.0 not in numbers


def test_spaced_persian_word_still_counts() -> None:
    numbers = [value for value, _, _ in extract_numbers("سه نفر کشته شدند")]
    assert 3.0 in numbers
    numbers = [value for value, _, _ in extract_numbers("دو نفر حاضر بودند")]
    assert 2.0 in numbers


# Y-6b: a ZWNJ-glued number + scale word is still a number and still matches.
def test_glued_number_scale_word_is_extracted() -> None:
    two_million = [(value, token, flags) for value, token, flags in extract_numbers("دو‌میلیون نفر")]
    assert (2_000_000.0, "دو‌میلیون", {"word", "scaled"}) in two_million
    three_thousand = [(value, token, flags) for value, token, flags in extract_numbers("سه‌هزار خانواده")]
    assert (3_000.0, "سه‌هزار", {"word", "scaled"}) in three_thousand


def test_glued_number_scale_word_matches_source() -> None:
    report = _check("۲۰۰۰۰۰۰ نفر آواره شدند", "گزارش شد.", "دو‌میلیون نفر آواره شدند")
    assert report["status"] == "clean"
    report = _check("۳۰۰۰ خانواده", "گزارش شد.", "سه‌هزار خانواده")
    assert report["status"] == "clean"


def test_glued_number_scale_word_absent_from_source_blocks() -> None:
    report = _check("۲۰۰۰۰۰۰ نفر آواره شدند", "گزارش شد.", "خانواده‌ها آواره شدند")
    assert report["status"] == "mismatch"
    assert report["blocking"] is True
    assert report["unmatched"][0]["reason_code"] == "not_in_source"
    report = _check("۳۰۰۰ خانواده", "گزارش شد.", "خانواده‌ها آواره شدند")
    assert report["status"] == "mismatch"
    assert report["unmatched"][0]["reason_code"] == "not_in_source"


def test_zwnj_nouns_are_still_not_numbers() -> None:
    for text in ("سه‌شنبه گذشته", "سه‌بعدی و دو‌جانبه"):
        values = [value for value, _, _ in extract_numbers(text)]
        assert 3.0 not in values
        assert 2.0 not in values

