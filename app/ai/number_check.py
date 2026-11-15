"""Deterministic numeric grounding check for Persian model output (Phase Y-1).

Every number that reaches ``headline_fa`` / ``summary_fa`` must also appear in the
exact text the model was given: the article title plus the body **after** the
truncation applied by :func:`app.ai.summarizer.build_analysis_prompt`.

The check is purely local: no model call, no network, no database. It is
deliberately conservative about what counts as evidence, and it labels every
non-match with a ``reason_code`` so the false-positive categories stay
measurable instead of being hidden behind one counter.
"""

from __future__ import annotations

import re
from typing import Any, Literal

# Single source of truth for the prompt truncation: summarizer.py imports this
# constant, so the checker and the prompt can never drift apart (acceptance Y-1.6).
MODEL_BODY_CHAR_LIMIT = 16000

NumberCheckMode = Literal["off", "log", "flag"]
ReasonCode = Literal[
    "not_in_source",
    "scale_mismatch",
    "jalali_calendar",
]

# Persian ۰-۹ and Arabic-Indic ٠-٩ digits folded to ASCII before any comparison.
_DIGIT_MAP = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")

_NUMBER_RE = re.compile(r"\d(?:[\d.,]*\d)?")
_THOUSANDS_RE = re.compile(r"^\d{1,3}(?:[.,]\d{3})+$")
_DECIMAL_RE = re.compile(r"^\d+[.,]\d{1,2}$")
_NEXT_WORD_RE = re.compile(r"\s*([^\W\d_]+)")

_SCALE_WORDS: dict[str, float] = {
    # Persian
    "هزار": 1_000,
    "میلیون": 1_000_000,
    "میلیارد": 1_000_000_000,
    "تریلیون": 1_000_000_000_000,
    # Arabic
    "ألف": 1_000,
    "الف": 1_000,
    "مليون": 1_000_000,
    "مليار": 1_000_000_000,
    # English
    "thousand": 1_000,
    "million": 1_000_000,
    "billion": 1_000_000_000,
    "trillion": 1_000_000_000_000,
}

# Spelled-out numbers in the source (Persian, Arabic, English). A model that writes
# "2" for a source "two" must not be punished for it (acceptance Y-1 category).
_WORD_NUMBERS: dict[str, float] = {
    # Persian
    "صفر": 0, "یک": 1, "دو": 2, "سه": 3, "چهار": 4, "پنج": 5, "شش": 6, "هفت": 7,
    "هشت": 8, "نه": 9, "ده": 10, "یازده": 11, "دوازده": 12, "سیزده": 13, "چهارده": 14,
    "پانزده": 15, "شانزده": 16, "هفده": 17, "هجده": 18, "نوزده": 19, "بیست": 20,
    "سی": 30, "چهل": 40, "پنجاه": 50, "شصت": 60, "هفتاد": 70, "هشتاد": 80,
    "نود": 90, "صد": 100, "هزار": 1_000, "میلیون": 1_000_000, "میلیارد": 1_000_000_000,
    # Arabic
    "واحد": 1, "اثنان": 2, "اثنين": 2, "ثلاثة": 3, "ثلاث": 3, "أربعة": 4, "أربع": 4,
    "خمسة": 5, "خمس": 5, "ستة": 6, "ست": 6, "سبعة": 7, "سبع": 7, "ثمانية": 8,
    "تسعة": 9, "تسع": 9, "عشرة": 10, "عشر": 10, "أحد عشر": 11, "اثنا عشر": 12,
    "عشرون": 20, "ثلاثون": 30, "أربعون": 40, "خمسون": 50, "ستون": 60, "سبعون": 70,
    "ثمانون": 80, "تسعون": 90, "مئة": 100, "مائة": 100, "مئتان": 200, "ألف": 1_000,
    "مليون": 1_000_000, "مليار": 1_000_000_000,
    # English
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
    "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16, "seventeen": 17,
    "eighteen": 18, "nineteen": 19, "twenty": 20, "thirty": 30, "forty": 40,
    "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90,
    "hundred": 100, "thousand": 1_000, "million": 1_000_000, "billion": 1_000_000_000,
}

_JALALI_RANGE = (1300.0, 1499.0)


def _prepare(text: str) -> str:
    """Fold digits and normalise separators before tokenising numbers."""
    prepared = (text or "").translate(_DIGIT_MAP)
    prepared = prepared.replace("\u066c", ",").replace("\u066b", ".")  # ٬ thousands, ٫ decimal
    # "1 200" (space as a thousands separator) is common in wire copy.
    prepared = re.sub(r"(?<=\d)[ \u00a0](?=\d{3}(?!\d))", "", prepared)
    return prepared


def _readings(token: str) -> set[float]:
    """All plausible values of one numeric token.

    ``1,200`` is accepted as both 1200 and 1.2: some feeds use a comma as the
    decimal mark, so the source side keeps both readings and a match on either
    one counts (acceptance Y-1.2).
    """
    token = token.replace("\u060c", ",").replace("\u066c", ",")
    readings: set[float] = set()
    if _THOUSANDS_RE.match(token):
        readings.add(float(re.sub(r"[.,]", "", token)))
        try:  # the same token may be a decimal comma reading, e.g. "1,200" -> 1.2
            readings.add(float(token.replace(",", ".")))
        except ValueError:
            pass
        return readings
    if _DECIMAL_RE.match(token):
        readings.add(float(token.replace(",", ".")))
        return readings
    for candidate in (token.replace(",", ""), token.replace(",", ".")):
        try:
            readings.add(float(candidate))
        except ValueError:  # pragma: no cover - only reachable for exotic separators
            continue
    return readings


def _fmt(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else repr(float(value)).rstrip("0").rstrip(".")


def _scale_after(prepared: str, end: int) -> tuple[float, tuple[int, int] | None]:
    """Return the unit multiplier after a number and the span of that unit word."""
    match = _NEXT_WORD_RE.match(prepared, end)
    if not match:
        return 1.0, None
    factor = _SCALE_WORDS.get(match.group(1).casefold(), 1.0)
    return factor, (match.start(1), match.end(1)) if factor != 1.0 else None


def _extract_occurrences(text: str) -> list[tuple[list[float], str, set[str]]]:
    """Group every numeric token with all of its readings.

    One token such as ``1,200`` yields several candidate values, but they are a
    single occurrence: matching any one of them settles it.
    """
    prepared = _prepare(text)
    occurrences: list[tuple[list[float], str, set[str]]] = []
    consumed: list[tuple[int, int]] = []
    for match in _NUMBER_RE.finditer(prepared):
        token = match.group(0)
        readings = _readings(token)
        scale, scale_span = _scale_after(prepared, match.end())
        if scale_span is not None:
            consumed.append(scale_span)
        flags: set[str] = set()
        if scale != 1.0:
            flags.add("scaled")
        if len(readings) > 1:
            flags.add("ambiguous")
        occurrences.append(([round(value * scale, 6) for value in readings], token, flags))

    for word, value in _WORD_NUMBERS.items():
        pattern = rf"(?<!\w){re.escape(word)}(?!\w)"
        for word_match in re.finditer(pattern, prepared, re.IGNORECASE):
            # A unit word already consumed by a preceding digit (۳ میلیون) is not a
            # stand-alone spelled-out number.
            if any(start <= word_match.start() and word_match.end() <= end for start, end in consumed):
                continue
            occurrences.append(([float(value)], word, {"word"}))
    return occurrences


def extract_numbers(text: str) -> list[tuple[float, str, set[str]]]:
    """Return ``(value, raw token, flags)`` for every reading of every number.

    ``flags`` may contain ``scaled`` (multiplied by a following unit word),
    ``ambiguous`` (several readings, e.g. ``1,200``) and ``word`` (spelled out).
    """
    return [
        (value, token, set(flags))
        for values, token, flags in _extract_occurrences(text)
        for value in values
    ]


def check_numbers(
    *,
    headline: str,
    summary: str,
    source_title: str,
    source_body: str,
    mode: NumberCheckMode = "log",
) -> dict[str, Any]:
    """Compare the numbers in the Persian output with the model's real input.

    Returns a JSON-serialisable report that the pipeline stores under
    ``articles.ai_payload["number_check"]``. ``blocking`` is True only when a
    number is unaccounted for and is not a plausible Jalali year; the caller
    decides what to do with it (``flag`` mode parks the article for review).
    """
    body = (source_body or "")[:MODEL_BODY_CHAR_LIMIT]
    base: dict[str, Any] = {
        "mode": mode,
        "checked_source_chars": len(body),
        "source_truncated": len(source_body or "") > MODEL_BODY_CHAR_LIMIT,
        "numbers_checked": 0,
        "matched": 0,
        "unmatched": [],
        "reason_codes": {},
        "status": "skipped",
        "blocking": False,
    }
    if mode == "off":
        return base

    source_items = _extract_occurrences(f"{source_title or ''}\n{body}")
    source_plain = {
        value
        for values, _, flags in source_items
        if "scaled" not in flags and "word" not in flags
        for value in values
    }
    source_scaled = {value for values, _, flags in source_items if "scaled" in flags for value in values}
    source_words = {value for values, _, flags in source_items if "word" in flags for value in values}
    source_ambiguous = {
        value for values, _, flags in source_items if "ambiguous" in flags for value in values
    }

    output_items = _extract_occurrences(f"{headline or ''}\n{summary or ''}")
    reason_codes: dict[str, int] = {}
    matched = 0
    unmatched: list[dict[str, str]] = []

    occurrences = output_items

    for values, token, flags in occurrences:
        if any(value in source_plain or value in source_scaled for value in values):
            matched += 1
            if "scaled" in flags:
                reason_codes["scale_converted"] = reason_codes.get("scale_converted", 0) + 1
            if "ambiguous" in flags or any(value in source_ambiguous for value in values):
                reason_codes["ambiguous_separator"] = reason_codes.get("ambiguous_separator", 0) + 1
            continue
        if any(value in source_words for value in values):
            matched += 1
            reason_codes["word_number_source"] = reason_codes.get("word_number_source", 0) + 1
            continue

        # Report the widest reading of the token: ``1,200`` is reported as 1200.
        key = max(values)
        if any(_JALALI_RANGE[0] <= value <= _JALALI_RANGE[1] for value in values):
            code: ReasonCode = "jalali_calendar"
        elif "scaled" in flags or source_scaled:
            code = "scale_mismatch"
        else:
            code = "not_in_source"
        reason_codes[code] = reason_codes.get(code, 0) + 1
        unmatched.append({"value": _fmt(key), "reason_code": code, "token": token})

    blocking = any(item["reason_code"] != "jalali_calendar" for item in unmatched)
    return {
        **base,
        "numbers_checked": len(occurrences),
        "matched": matched,
        "unmatched": unmatched,
        "reason_codes": reason_codes,
        "status": "mismatch" if unmatched else "clean",
        "blocking": blocking,
    }
