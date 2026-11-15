"""Deterministic numeric grounding check for Persian model output (Phase Y-1/Y-4).

Every number that reaches ``headline_fa`` / ``summary_fa`` must also appear in the
exact text the model was given: the article title plus the body **after** the
truncation applied by :func:`app.ai.summarizer.build_analysis_prompt`.

The check is purely local: no model call, no network, no database.

Parsing is attempted before anything is labelled (Y-4):
  * spelled-out compounds -- ``twenty-three``, ``twenty three``, «بیست و سه»;
  * attached/short scale units -- ``3bn``, ``2.5m``, ``5k``, ``مليونًا``;
  * ``m`` is ambiguous (metre vs million), so both readings are kept and a match
    on either one settles the number.
Only a token that survives all of that as *unparsed* can produce the
``unverifiable`` reason code, and ``unverifiable`` never blocks an article.
"""

from __future__ import annotations

import logging
import re
from bisect import bisect_right
from typing import Any, Literal

logger = logging.getLogger(__name__)

# Single source of truth for the prompt truncation: summarizer.py imports this
# constant, so the checker and the prompt can never drift apart (acceptance Y-1.6).
MODEL_BODY_CHAR_LIMIT = 16000

NumberCheckMode = Literal["off", "log", "flag"]
ReasonCode = Literal[
    "not_in_source",
    "scale_mismatch",
    "jalali_calendar",
    "unverifiable",
    "arabic_dual_possible",
]
# Codes that may park an article for review. Everything else is informational.
BLOCKING_CODES = frozenset({"not_in_source", "scale_mismatch"})

# Persian ۰-۹ and Arabic-Indic ٠-٩ digits folded to ASCII before any comparison.
_DIGIT_MAP = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")
_DIACRITICS_RE = re.compile(r"[\u064b-\u0652]")

_NUMBER_RE = re.compile(r"\d(?:[\d.,]*\d)?")
_THOUSANDS_RE = re.compile(r"^\d{1,3}(?:[.,]\d{3})+$")
_DECIMAL_RE = re.compile(r"^\d+[.,]\d{1,2}$")
_NEXT_WORD_RE = re.compile(r"[\s\u200c\u0640]*([^\W\d_]+)")

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

# Attached scale units: "3bn" in wire copy is "3 billion". "m" is kept ambiguous
# because in the same corpus it can be metres, million, or a person's initial.
_SHORT_UNITS: dict[str, float] = {
    "bn": 1_000_000_000,
    "bln": 1_000_000_000,
    "b": 1_000_000_000,
    "tn": 1_000_000_000_000,
    "trn": 1_000_000_000_000,
    "k": 1_000,
    "mn": 1_000_000,
    "mm": 1_000_000,
}
_AMBIGUOUS_SHORT_UNITS: dict[str, float] = {"m": 1_000_000}
# Inflected Arabic/Persian scale words: مليونًا, میلیونی, مليارات ...
_SCALE_STEMS: tuple[tuple[str, float], ...] = (
    ("میلیارد", 1_000_000_000),
    ("میلیون", 1_000_000),
    ("مليار", 1_000_000_000),
    ("مليون", 1_000_000),
    ("ميليون", 1_000_000),
    ("هزار", 1_000),
)
_ARABIC_PLURALS: dict[str, float] = {
    "ملايين": 1_000_000,
    "مليارات": 1_000_000_000,
    "آلاف": 1_000,
    "الاف": 1_000,
    "الآف": 1_000,
}

_TENS = {
    "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60,
    "seventy": 70, "eighty": 80, "ninety": 90,
    "بیست": 20, "سی": 30, "چهل": 40, "پنجاه": 50, "شصت": 60,
    "هفتاد": 70, "هشتاد": 80, "نود": 90,
}
_UNITS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
    "eight": 8, "nine": 9,
    "یک": 1, "دو": 2, "سه": 3, "چهار": 4, "پنج": 5, "شش": 6, "هفت": 7,
    "هشت": 8, "نه": 9,
}

# Spelled-out numbers in the source (Persian, Arabic, English). A model that writes
# "2" for a source "two" must not be punished for it.
_WORD_NUMBERS: dict[str, float] = {
    "صفر": 0, "یک": 1, "دو": 2, "سه": 3, "چهار": 4, "پنج": 5, "شش": 6, "هفت": 7,
    "هشت": 8, "نه": 9, "ده": 10, "یازده": 11, "دوازده": 12, "سیزده": 13, "چهارده": 14,
    "پانزده": 15, "شانزده": 16, "هفده": 17, "هجده": 18, "نوزده": 19, "بیست": 20,
    "سی": 30, "چهل": 40, "پنجاه": 50, "شصت": 60, "هفتاد": 70, "هشتاد": 80,
    "نود": 90, "صد": 100, "هزار": 1_000, "میلیون": 1_000_000, "میلیارد": 1_000_000_000,
    "واحد": 1, "اثنان": 2, "اثنين": 2, "ثلاثة": 3, "ثلاث": 3, "أربعة": 4, "أربع": 4,
    "خمسة": 5, "خمس": 5, "ستة": 6, "ست": 6, "سبعة": 7, "سبع": 7, "ثمانية": 8,
    "تسعة": 9, "تسع": 9, "عشرة": 10, "عشر": 10, "عشرون": 20, "ثلاثون": 30,
    "أربعون": 40, "خمسون": 50, "ستون": 60, "سبعون": 70, "ثمانون": 80, "تسعون": 90,
    "مئة": 100, "مائة": 100, "مئتان": 200,
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
    "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16, "seventeen": 17,
    "eighteen": 18, "nineteen": 19, "twenty": 20, "thirty": 30, "forty": 40,
    "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90,
    "hundred": 100, "thousand": 1_000, "million": 1_000_000, "billion": 1_000_000_000,
}

# ── Y-5: ambiguous Persian words, bare scales, and Arabic spelled-out numbers ──

# «یک» (one), «نه» (no / nine) and «واحد» (unit / one) are ordinary words far more
# often than they are figures, and a scale word with no number before it
# («میلیارد») carries no value at all. Each counts only when it sits immediately
# before a scale word or «درصد». Documented limitation: a plain "یک نفر کشته شد"
# is no longer checked (see docs/DEVELOPMENT_LOG.md, phase Y-5).
_AMBIGUOUS_ALONE: dict[str, int] = {"یک": 1, "نه": 9, "واحد": 1}
_PERCENT_WORD = "درصد"
_AMBIGUOUS_TAILS = (_PERCENT_WORD, *_SCALE_WORDS.keys())
_AMBIGUOUS_RE = re.compile(
    rf"(?<!\w)(?P<word>{'|'.join(map(re.escape, _AMBIGUOUS_ALONE))})(?:[\s\u200c]+)"
    rf"(?P<tail>{'|'.join(map(re.escape, sorted(_AMBIGUOUS_TAILS, key=len, reverse=True)))})(?!\w)"
)

# Arabic spelled-out numbers. Keys are folded by :func:`_fold_arabic`, so «ثلاثة»
# and «ثلاثه» resolve to the same entry; feminine and short forms are listed.
_ALEF_FOLD = str.maketrans({"أ": "ا", "إ": "ا", "آ": "ا", "ٱ": "ا", "ة": "ه", "ى": "ي", "ـ": ""})
_ARABIC_UNITS: dict[str, float] = {
    "صفر": 0,
    "واحد": 1, "واحده": 1, "احد": 1, "احدي": 1,
    "اثنان": 2, "اثنين": 2, "اثنتان": 2, "اثنتين": 2, "ثنتان": 2, "ثنتين": 2,
    "ثلاثه": 3, "ثلاث": 3,
    "اربعه": 4, "اربع": 4,
    "خمسه": 5, "خمس": 5,
    "سته": 6, "ست": 6,
    "سبعه": 7, "سبع": 7,
    "ثمانيه": 8, "ثمان": 8, "ثماني": 8,
    "تسعه": 9, "تسع": 9,
    "عشره": 10, "عشر": 10,
}
_ARABIC_TENS: dict[str, float] = {
    "عشرون": 20, "عشرين": 20,
    "ثلاثون": 30, "ثلاثين": 30,
    "اربعون": 40, "اربعين": 40,
    "خمسون": 50, "خمسين": 50,
    "ستون": 60, "ستين": 60,
    "سبعون": 70, "سبعين": 70,
    "ثمانون": 80, "ثمانين": 80,
    "تسعون": 90, "تسعين": 90,
}
_ARABIC_HUNDREDS: dict[str, float] = {
    "مئه": 100, "مائه": 100, "مئتان": 200, "مئتين": 200, "مئتا": 200, "مئتي": 200,
    "ثلاثمئه": 300, "ثلاثمائه": 300, "اربعمئه": 400, "خمسمئه": 500,
    "ستمئه": 600, "سبعمئه": 700, "ثمانمئه": 800, "ثمانمائه": 800, "تسعمئه": 900,
}
_ARABIC_SCALES: dict[str, float] = {
    "الف": 1_000, "الفان": 2_000, "الفين": 2_000, "الاف": 1_000,
    "مليون": 1_000_000, "مليونان": 2_000_000, "مليونين": 2_000_000, "ملايين": 1_000_000,
    "مليار": 1_000_000_000, "ملياران": 2_000_000_000, "مليارين": 2_000_000_000,
    "مليارات": 1_000_000_000,
}
# (kind, value): "part" words are added together, "scale" words multiply what is
# accumulated so far -- «مئة ألف» is 100 * 1000 and «خمسة عشر» is 5 + 10.
_ARABIC_PARTS: dict[str, tuple[str, float]] = {
    **{word: ("part", value) for word, value in _ARABIC_UNITS.items()},
    **{word: ("part", value) for word, value in _ARABIC_TENS.items()},
    **{word: ("part", value) for word, value in _ARABIC_HUNDREDS.items()},
    **{word: ("scale", value) for word, value in _ARABIC_SCALES.items()},
}
_WORD_TOKEN_RE = re.compile(r"[^\W\d_]+")
# Words inside one Arabic number are separated by spaces, ZWNJ, tatweel, or a
# glued conjunction «و» («ثلاثة وعشرون»).
_ARABIC_JOIN_RE = re.compile(r"[\s\u200c\u0640]*(?:و[\s\u200c\u0640]*)?")

_JALALI_RANGE = (1300.0, 1499.0)
# Which scale an unreadable unit might mean. The South-Asian lakh/crore are included
# because wires still quote them; the point is to avoid calling such a number invented.
_SCALE_GUESSES = (
    1.0,
    1_000.0,
    100_000.0,
    1_000_000.0,
    10_000_000.0,
    1_000_000_000.0,
    1_000_000_000_000.0,
)


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


def _unit_factors(word: str) -> tuple[list[float], bool] | None:
    """Multipliers carried by a unit word, or None when it is not a unit.

    The second element marks an ambiguous unit (``m``), where both the bare number
    and the scaled number are valid readings.
    """
    cleaned = _DIACRITICS_RE.sub("", (word or "").strip())
    folded = _fold_arabic(cleaned).casefold()
    if not folded:
        return None
    if folded in _SCALE_WORDS:
        return [float(_SCALE_WORDS[folded])], False
    if folded in _SHORT_UNITS:
        return [float(_SHORT_UNITS[folded])], False
    if folded in _AMBIGUOUS_SHORT_UNITS:
        return [1.0, float(_AMBIGUOUS_SHORT_UNITS[folded])], True
    if folded in _ARABIC_PLURALS:
        return [float(_ARABIC_PLURALS[folded])], False
    for stem, factor in _SCALE_STEMS:
        if folded.startswith(stem):
            return [float(factor)], False
    return None


def _looks_like_unit(word: str) -> bool:
    """Whether an unknown token after a number is shaped like a unit, not a word."""
    return len(word) <= 4 and word.isascii() and word.isalpha()


def _scale_after(prepared: str, end: int) -> tuple[list[float] | None, tuple[int, int] | None, bool]:
    """Return (factors, span, ambiguous) for the unit word after a number."""
    match = _NEXT_WORD_RE.match(prepared, end)
    if not match:
        return [1.0], None, False
    word = match.group(1)
    parsed = _unit_factors(word)
    if parsed is None:
        return None, (match.start(1), match.end(1)), False
    factors, ambiguous = parsed
    if factors == [1.0] and not ambiguous:
        return factors, None, False
    return factors, (match.start(1), match.end(1)), ambiguous


def _fold_arabic(word: str) -> str:
    """Strip diacritics and unify alef/ta-marbuta/alef-maqsura for lexicon lookups."""
    return _DIACRITICS_RE.sub("", word or "").strip().translate(_ALEF_FOLD)


def _without_article(folded: str) -> str:
    """Drop the Arabic definite article: «العشرين» -> «عشرين»."""
    return folded[2:] if folded.startswith("ال") and len(folded) > 2 else folded


def _in_any(position: int, spans: list[tuple[int, int]]) -> bool:
    """Whether a position falls inside one of the spans, in O(log n).

    ``spans`` must be sorted by start: pathological input produces thousands of
    them, and a linear scan per token made the check quadratic (Y-5).
    """
    index = bisect_right(spans, (position, float("inf"))) - 1
    return index >= 0 and spans[index][1] > position


def _ambiguous_scaled_words(
    prepared: str,
) -> tuple[list[tuple[list[float], str, set[str]]], list[tuple[int, int]]]:
    """«یک میلیون» / «نه درصد» -- an ambiguous word that *is* a number here."""
    found: list[tuple[list[float], str, set[str]]] = []
    spans: list[tuple[int, int]] = []
    for match in _AMBIGUOUS_RE.finditer(prepared):
        value = float(_AMBIGUOUS_ALONE[match.group("word")])
        tail = match.group("tail")
        if tail == _PERCENT_WORD:
            found.append(([value], match.group(0), {"word"}))
        else:
            found.append(
                ([round(value * float(_SCALE_WORDS[tail]), 6)], match.group(0), {"word", "scaled"})
            )
        spans.append((match.start(), match.end()))
    return found, spans


def _arabic_part(token: str, *, source_side: bool) -> tuple[str, float] | None:
    """Lexicon entry for one Arabic number word, or None when it is not one."""
    folded = _fold_arabic(token)
    if not source_side and folded in _AMBIGUOUS_ALONE:
        # On the output side «واحد» alone is the ordinary word "unit".
        return None
    part = _ARABIC_PARTS.get(folded)
    if part is None and folded.startswith("و"):
        part = _ARABIC_PARTS.get(folded[1:])  # glued conjunction: «وعشرون»
    return part or _ARABIC_PARTS.get(_without_article(folded))


def _arabic_word_numbers(
    prepared: str, consumed: list[tuple[int, int]], *, source_side: bool
) -> tuple[list[tuple[float, str]], list[tuple[int, int]]]:
    """Parse Arabic spelled-out numbers into single values (Y-5).

    A token outside the lexicon is treated as ordinary text, never as a lenient
    wildcard: only a number we can name may cover an output figure (Y-5 review).
    """
    tokens = list(_WORD_TOKEN_RE.finditer(prepared))
    items: list[tuple[float, str]] = []
    spans: list[tuple[int, int]] = []
    index = 0
    while index < len(tokens):
        first = tokens[index]
        if _in_any(first.start(), consumed):
            index += 1
            continue
        parts: list[tuple[str, float]] = []
        start = first.start()
        end = first.end()
        cursor = index
        while cursor < len(tokens):
            token_match = tokens[cursor]
            if parts and not _ARABIC_JOIN_RE.fullmatch(prepared[end:token_match.start()]):
                break
            part = _arabic_part(token_match.group(0), source_side=source_side)
            if part is None:
                break
            parts.append(part)
            end = token_match.end()
            cursor += 1
        if not parts:
            index += 1
            continue
        # A scale word with no number before it is not a figure («میلیارد» alone).
        if len(parts) == 1 and parts[0][0] == "scale" and not source_side:
            index = cursor
            continue
        value = 0.0
        for kind, part_value in parts:
            value = (value or 1.0) * part_value if kind == "scale" else value + part_value
        items.append((round(value, 6), prepared[start:end]))
        spans.append((start, end))
        index = cursor
    return items, spans


def _compound_word_numbers(prepared: str) -> tuple[list[tuple[float, str]], list[tuple[int, int]]]:
    """Parse ``twenty-three`` / «بیست و سه» into a single value.

    Returns the parsed occurrences and the spans they occupy so the individual
    words are not counted a second time.
    """
    tens_alt = "|".join(re.escape(word) for word in _TENS)
    units_alt = "|".join(re.escape(word) for word in _UNITS)
    pattern = re.compile(
        rf"(?<!\w)(?P<tens>{tens_alt})(?:\s*[-\u2010\u2011]\s*|\s+(?:and|و)\s+|\s+)"
        rf"(?P<unit>{units_alt})(?!\w)",
        re.IGNORECASE,
    )
    found: list[tuple[float, str]] = []
    spans: list[tuple[int, int]] = []
    for match in pattern.finditer(prepared):
        value = _TENS[match.group("tens").casefold()] + _UNITS[match.group("unit").casefold()]
        found.append((float(value), match.group(0)))
        spans.append((match.start(), match.end()))
    return found, spans


def _extract(
    text: str, *, source_side: bool = False
) -> tuple[
    list[tuple[list[float], str, set[str]]],
    list[tuple[float, str]],
]:
    """Return (occurrences, unparsed_units).

    ``source_side`` keeps the source lenient: bare «یک»/«واحد» and bare scale words
    still count as source numbers there, they are only dropped from the output.
    """
    prepared = _prepare(text)
    occurrences: list[tuple[list[float], str, set[str]]] = []
    unparsed_units: list[tuple[float, str]] = []
    consumed: list[tuple[int, int]] = []

    compounds, compound_spans = _compound_word_numbers(prepared)
    for value, token in compounds:
        occurrences.append(([value], token, {"word"}))
    consumed.extend(compound_spans)

    scaled_words, scaled_spans = _ambiguous_scaled_words(prepared)
    occurrences.extend(scaled_words)
    consumed.extend(scaled_spans)

    for match in _NUMBER_RE.finditer(prepared):
        token = match.group(0)
        readings = _readings(token)
        factors, unit_span, ambiguous_unit = _scale_after(prepared, match.end())
        flags: set[str] = set()
        if len(readings) > 1:
            flags.add("ambiguous")
        if unit_span is not None:
            consumed.append(unit_span)
            word = prepared[unit_span[0]:unit_span[1]]
            if factors is None:
                # A token shaped like a unit that we cannot read: remember it so an
                # output number built from it can be called unverifiable rather than
                # fabricated (Y-4).
                if _looks_like_unit(word):
                    for value in readings:
                        unparsed_units.append((value, word))
                factors = [1.0]
            else:
                flags.add("scaled")
                if ambiguous_unit:
                    flags.add("ambiguous_unit")
        for factor in factors or [1.0]:
            occurrences.append(([round(value * factor, 6) for value in readings], token, set(flags)))

    consumed.sort()  # _in_any() bisects this list
    arabic_items, arabic_spans = _arabic_word_numbers(prepared, consumed, source_side=source_side)
    for value, token in arabic_items:
        occurrences.append(([value], token, {"word"}))
    consumed.extend(arabic_spans)
    consumed.sort()

    # Words that are only numbers in a numeric phrase (see _AMBIGUOUS_ALONE) and
    # bare scale words never count on their own in the Persian output.
    alone_words = set() if source_side else _AMBIGUOUS_ALONE.keys() | _SCALE_WORDS.keys()
    for word, value in _WORD_NUMBERS.items():
        if word in alone_words:
            continue
        # «سه»/«دو» that are glued with ZWNJ to another letter («سه‌شنبه»,
        # «دو‌جانبه») are part of a compound word, not a figure (Y-6 pattern ج).
        if word in ("سه", "دو"):
            pattern = rf"(?<!\w){re.escape(word)}(?![\w\u200c])"
        else:
            pattern = rf"(?<!\w){re.escape(word)}(?!\w)"
        for word_match in re.finditer(pattern, prepared, re.IGNORECASE):
            if _in_any(word_match.start(), consumed):
                continue
            occurrences.append(([float(value)], word, {"word"}))

    return occurrences, unparsed_units


def extract_numbers(text: str) -> list[tuple[float, str, set[str]]]:
    """Return ``(value, raw token, flags)`` for every reading of every number."""
    occurrences, _ = _extract(text)
    return [
        (value, token, set(flags))
        for values, token, flags in occurrences
        for value in values
    ]


_ARABIC_LETTER_RANGE = ("\u0600", "\u06ff")
_DUAL_SUFFIXES = ("ان", "ين", "تان", "تين")


def _arabic_letter_ratio(text: str) -> float:
    """Share of Arabic-script letters among all letters (Y-6 pattern الف)."""
    letters = [ch for ch in (text or "") if ch.isalpha()]
    if not letters:
        return 0.0
    arabic = sum(1 for ch in letters if "\u0600" <= ch <= "\u06ff")
    return arabic / len(letters)


def _has_arabic_dual_token(text: str) -> bool:
    """Whether the text holds a word with an Arabic dual ending (Y-6 pattern الف)."""
    for match in _WORD_TOKEN_RE.finditer(text or ""):
        word = match.group(0)
        if len(word) >= 4 and word.endswith(_DUAL_SUFFIXES):
            return True
    return False


def _is_unverifiable(values: list[float], unparsed_units: list[tuple[float, str]]) -> bool:
    """Whether an unmatched number could come from a source token we could not parse.

    Only the unit-based reading applies (``3 lakh``). There is no text-wide fallback:
    an unmatched figure is ``unverifiable`` only when a source token *of that value*
    could have produced it, so an unrelated word in the article can never excuse it
    (Y-5 review).
    """
    for prefix, _unit in unparsed_units:
        for guess in _SCALE_GUESSES:
            candidate = round(prefix * guess, 6)
            if any(abs(value - candidate) < 1e-6 for value in values):
                return True
    return False


ERROR_MESSAGE_MAX_CHARS = 200


def safe_check_numbers(
    *,
    headline: str,
    summary: str,
    source_title: str,
    source_body: str,
    mode: NumberCheckMode = "log",
) -> dict[str, Any]:
    """Run :func:`check_numbers` without ever breaking article processing (Y-1b).

    Only ``Exception`` is caught on purpose: cancellation and interpreter-level
    exits must propagate. A failure is reported as ``status="error"``, is never
    blocking, and leaves the article on its normal successful path.
    """
    try:
        return check_numbers(
            headline=headline,
            summary=summary,
            source_title=source_title,
            source_body=source_body,
            mode=mode,
        )
    except Exception as exc:  # noqa: BLE001 - deliberate boundary around a local check
        logger.exception("Number grounding check failed; continuing without it: %s", exc)
        return {
            "mode": mode,
            "checked_source_chars": 0,
            "source_truncated": False,
            "numbers_checked": 0,
            "matched": 0,
            "unmatched": [],
            "reason_codes": {},
            "status": "error",
            "blocking": False,
            "error": f"{type(exc).__name__}: {exc}"[:ERROR_MESSAGE_MAX_CHARS],
        }


def check_numbers(
    *,
    headline: str,
    summary: str,
    source_title: str,
    source_body: str,
    mode: NumberCheckMode = "log",
) -> dict[str, Any]:
    """Compare the numbers in the Persian output with the model's real input.

    Returns a JSON-serialisable report stored under ``articles.ai_payload["number_check"]``.
    ``blocking`` is only True for codes in :data:`BLOCKING_CODES`.
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

    source_items, unparsed_units = _extract(f"{source_title or ''}\n{body}", source_side=True)
    source_plain = {
        value
        for values, _, flags in source_items
        if "scaled" not in flags and "word" not in flags
        for value in values
    }
    source_scaled = {value for values, _, flags in source_items if "scaled" in flags for value in values}
    source_words = {value for values, _, flags in source_items if "word" in flags for value in values}
    source_ambiguous = {value for values, _, flags in source_items if "ambiguous" in flags for value in values}

    output_items, _ = _extract(f"{headline or ''}\n{summary or ''}", source_side=False)
    source_text = f"{source_title or ''}\n{body}"
    reason_codes: dict[str, int] = {}
    matched = 0
    unmatched: list[dict[str, str]] = []

    for values, token, flags in output_items:
        if any(value in source_plain or value in source_scaled for value in values):
            matched += 1
            if "scaled" in flags:
                reason_codes["scale_converted"] = reason_codes.get("scale_converted", 0) + 1
            if "ambiguous_unit" in flags:
                reason_codes["ambiguous_unit"] = reason_codes.get("ambiguous_unit", 0) + 1
            if "ambiguous" in flags or any(value in source_ambiguous for value in values):
                reason_codes["ambiguous_separator"] = reason_codes.get("ambiguous_separator", 0) + 1
            continue
        if any(value in source_words for value in values):
            matched += 1
            reason_codes["word_number_source"] = reason_codes.get("word_number_source", 0) + 1
            continue

        # Report the widest reading of the token: ``1,200`` is reported as 1200.
        key = max(values)
        if (
            len(values) == 1
            and abs(values[0] - 2.0) < 1e-6
            and _arabic_letter_ratio(source_text) >= 0.5
            and _has_arabic_dual_token(source_text)
        ):
            code = "arabic_dual_possible"
        elif _is_unverifiable(values, unparsed_units):
            code: ReasonCode = "unverifiable"
        elif any(_JALALI_RANGE[0] <= value <= _JALALI_RANGE[1] for value in values):
            code = "jalali_calendar"
        elif "scaled" in flags:
            code = "scale_mismatch"
        else:
            code = "not_in_source"
        reason_codes[code] = reason_codes.get(code, 0) + 1
        unmatched.append({"value": _fmt(key), "reason_code": code, "token": token})

    blocking = any(item["reason_code"] in BLOCKING_CODES for item in unmatched)
    # Y-5: "mismatch" is reserved for a real disagreement with the source. An article
    # whose only findings are informational (jalali_calendar, unverifiable) is
    # "unverifiable" instead, so it no longer pollutes the mismatch statistics.
    if not unmatched:
        status = "clean"
    elif blocking:
        status = "mismatch"
    else:
        status = "unverifiable"
    return {
        **base,
        "numbers_checked": len(output_items),
        "matched": matched,
        "unmatched": unmatched,
        "reason_codes": reason_codes,
        "status": status,
        "blocking": blocking,
    }
