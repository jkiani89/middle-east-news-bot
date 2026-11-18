"""Headline hygiene for a published news block.

A feed sometimes ships a title that is only the outlet's own name (``- رادیو فردا``)
because the item arrived without a headline. Publishing that string as the headline of a
news block tells the reader nothing, and the generic ``خبر بدون عنوان`` fallback never
fires for it because the string is not empty.

This module decides whether a headline carries any information and, when it does not,
picks the best available replacement in a fixed order: the event title, then the first
sentence of the summary, then a source-and-time label. The choice is deterministic and
reports *why* the original was rejected, so callers can count the substitutions instead of
hiding them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

# A headline shorter than this carries no readable statement on its own.
MIN_HEADLINE_CHARS = 8
MAX_FALLBACK_CHARS = 110

REASON_OK = "ok"
REASON_EMPTY = "empty"
REASON_JUNK = "junk"
REASON_SOURCE_LABEL = "source_label"
REASON_TOO_SHORT = "too_short"

_SEPARATOR_CHARS = " \t\r\n-–—ـ_|/:،,؛;·•*+=~<>\"'«»“”‘’()[]{}…"
_WHITESPACE_RE = re.compile(r"\s+")
_SENTENCE_SPLIT_RE = re.compile(r"[.!؟?؛;\n]+")
_PLACEHOLDER_TITLES = frozenset(
    {
        "بدون عنوان",
        "خبر بدون عنوان",
        "بی عنوان",
        "بدون تیتر",
        "خبر بدون تیتر",
        "خبر",
        "خبر جدید",
        "نامشخص",
        "untitled",
        "no title",
        "none",
        "null",
        "nan",
        "rss",
        "news",
        "item",
        "home",
    }
)

# Outlet names that appear as a whole feed title when an item has no headline. Stored
# compacted (no spaces, no punctuation, lower case) so "- رادیو فردا" and "Radio Farda"
# both match their entry. This is a closed list of the outlets this project reads.
_OUTLET_LABELS = (
    "رادیوفردا",
    "radiofarda",
    "ایراناینترنشنال",
    "iraninternational",
    "بیبیسی",
    "bbc",
    "bbcpersian",
    "الجزیره",
    "aljazeera",
    "العربیه",
    "alarabiya",
    "الحره",
    "alhurra",
    "دویچهوله",
    "dw",
    "deutschewelle",
    "یورونیوز",
    "euronews",
    "رویترز",
    "reuters",
    "اسوشیتدپرس",
    "associatedpress",
    "apnews",
    "نیویورکتایمز",
    "newyorktimes",
    "واشینگتنپست",
    "washingtonpost",
    "فرانس۲۴",
    "france24",
    "اسکاینیوز",
    "skynews",
    "سیانان",
    "cnn",
    "فاکسنیوز",
    "foxnews",
    "اسپوتنیک",
    "sputnik",
    "راشاتودی",
    "rttv",
    "تسنیم",
    "tasnim",
    "ایرنا",
    "irna",
    "مهر",
    "mehrnews",
    "فارس",
    "farsnews",
    "ایسنا",
    "isna",
    "ایلنا",
    "ilna",
    "شرق",
    "sharghdaily",
    "همشهری",
    "hamshahri",
    "کیهان",
    "kayhan",
    "اعتماد",
    "etemad",
    "المانیتور",
    "almonitor",
    "میدلایستآی",
    "middleeasteye",
    "تایمزاسرائیل",
    "timesofisrael",
    "هاآرتص",
    "haaretz",
    "اورشلیمپست",
    "jerusalempost",
    "jpost",
    "النشنال",
    "thenational",
    "گلفنیوز",
    "gulfnews",
    "عربنیوز",
    "arabnews",
    "آناتولی",
    "anadolu",
    "aa",
)


@dataclass(frozen=True)
class Headline:
    """The headline to publish plus why the supplied one was not used."""

    text: str
    reason: str

    @property
    def replaced(self) -> bool:
        return self.reason != REASON_OK


def _persian_normalised(value: str) -> str:
    """Unify Arabic/Persian letter variants and collapse whitespace."""
    text = (value or "").replace("ي", "ی").replace("ك", "ک").replace("ة", "ه").replace("ـ", "")
    return _WHITESPACE_RE.sub(" ", text).strip()


def _strip_separators(value: str) -> str:
    """Remove leading/trailing separators and quotation marks, repeatedly."""
    text = value or ""
    previous = None
    while text != previous:
        previous = text
        text = text.strip().strip(_SEPARATOR_CHARS)
    return text


def _clean(value: Any) -> str:
    return _strip_separators(_persian_normalised(str(value or "")))


def _compact(value: str) -> str:
    """Aggressive form used only to compare a title with a bare outlet name."""
    text = _persian_normalised(value).lower()
    return re.sub(r"[\s\-–—_|/:،,؛;·•*+=~<>\"'«»“”‘’()\[\]{}…]+", "", text)


def _outlet_labels() -> set[str]:
    return {_compact(label) for label in _OUTLET_LABELS}


def _is_outlet_label(text: str, source: str | None) -> bool:
    compact = _compact(text)
    if not compact:
        return False
    if compact in _outlet_labels():
        return True
    return bool(source) and compact == _compact(source)


def first_sentence(value: Any) -> str:
    """First sentence of a body/summary, used as a headline of last resort."""
    text = _persian_normalised(str(value or ""))
    if not text:
        return ""
    sentence = _SENTENCE_SPLIT_RE.split(text, maxsplit=1)[0].strip()
    return sentence[:MAX_FALLBACK_CHARS].strip()


def headline_rejection(value: Any, source: str | None = None) -> str:
    """Why this headline is unusable, or :data:`REASON_OK` when it is fine."""
    text = _clean(value)
    if not text:
        return REASON_EMPTY
    if text.lower() in _PLACEHOLDER_TITLES:
        return REASON_JUNK
    if _is_outlet_label(text, source):
        return REASON_SOURCE_LABEL
    if len(text) < MIN_HEADLINE_CHARS or len(text.split()) < 2:
        return REASON_TOO_SHORT
    return REASON_OK


def headline_is_valid(value: Any, source: str | None = None) -> bool:
    return headline_rejection(value, source) == REASON_OK


def sanitize_headline(
    raw: Any,
    *,
    source: str | None = None,
    alt: Any = None,
    summary: Any = None,
    local_time: str | None = None,
    fallback: str = "خبر بدون عنوان",
) -> Headline:
    """Return a publishable headline and the reason the original could not be used.

    The replacement order is fixed, so the same stored item always renders the same
    headline: the raw title, the event/alternative title, the first summary sentence, a
    source-and-time label, and finally the generic fallback.
    """
    reason = headline_rejection(raw, source)
    if reason == REASON_OK:
        return Headline(text=_clean(raw), reason=REASON_OK)

    alt_text = _clean(alt)
    if headline_is_valid(alt_text, source):
        return Headline(text=alt_text, reason=reason)

    summary_text = first_sentence(summary)
    if headline_is_valid(summary_text, source):
        return Headline(text=summary_text, reason=reason)

    label = _persian_normalised(str(source or "")).strip()
    if label and local_time:
        return Headline(text=f"خبر {label} — {local_time}", reason=reason)
    if label:
        return Headline(text=f"خبر {label}", reason=reason)
    if local_time:
        return Headline(text=f"خبر بدون عنوان — {local_time}", reason=reason)
    return Headline(text=fallback, reason=reason)
