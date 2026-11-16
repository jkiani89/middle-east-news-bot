"""Persian orthography normalisation and Arabic-vs-Persian discrimination (Phase Y-3).

The language guard in :mod:`app.ai.summarizer` rejects output that uses Arabic
orthography (``ي ك ى``). Before spending a schema-repair call on such text, the
letters are normalised — but only when the field is genuinely Persian. The
discriminator is deliberately **not** the Arabic-only ratio: measurement on real
paragraphs showed an Arabic-orthography Persian sentence scoring 0.106, inside
the 0.099–0.156 band of fully Arabic paragraphs (see docs/DEVELOPMENT_LOG.md).

Instead two independent signals are combined:

* at least one Persian-specific letter (``پ چ ژ گ``), which Arabic news copy
  essentially never contains, and
* the density of Arabic marker tokens (``ال``-prefixed words plus the
  unambiguous function words ``في على التي الذي``) staying under a ceiling, so an
  untranslated Arabic paragraph that happens to contain one Persian letter is
  still left alone and rejected by the guard.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Arabic-only letters never used in Persian orthography (shared with the guard).
ARABIC_ORTHOGRAPHY_LETTERS = frozenset("\u064a\u0643\u0629\u0649")
# ي→ی, ك→ک, ى→ی. «ة» is intentionally left untouched.
_TO_PERSIAN = str.maketrans({"\u064a": "\u06cc", "\u0643": "\u06a9", "\u0649": "\u06cc"})
PERSIAN_SPECIFIC_LETTERS = frozenset("پچژگ")
# Unambiguous in Arabic and absent from normal Persian prose ("من" is excluded on
# purpose: it is an everyday Persian pronoun and would inflate the density).
_ARABIC_MARKERS = frozenset({"في", "على", "التي", "الذي", "الذين", "إلى", "الى", "هذا", "هذه", "ذلك"})
_WORD_RE = re.compile(r"[^\W\d_]+", re.UNICODE)
MAX_ARABIC_MARKER_DENSITY = 0.25
MIN_PERSIAN_SPECIFIC_LETTERS = 1


@dataclass(frozen=True)
class OrthographyMetrics:
    """Letter and token counts for one text field."""

    letters: int
    arabic_only: int
    persian_specific: int
    tokens: int
    arabic_marker_tokens: int

    @property
    def arabic_marker_density(self) -> float:
        return self.arabic_marker_tokens / self.tokens if self.tokens else 0.0

    def as_dict(self) -> dict[str, float | int]:
        return {
            "letters": self.letters,
            "arabic_only_letters": self.arabic_only,
            "persian_specific_letters": self.persian_specific,
            "tokens": self.tokens,
            "arabic_marker_tokens": self.arabic_marker_tokens,
            "arabic_marker_density": round(self.arabic_marker_density, 4),
        }


def measure(text: str) -> OrthographyMetrics:
    """Count letters and Arabic marker tokens without changing the text."""
    letters = [ch for ch in (text or "") if ch.isalpha()]
    tokens = _WORD_RE.findall((text or "").casefold())
    marker_tokens = 0
    for token in tokens:
        if token in _ARABIC_MARKERS:
            marker_tokens += 1
        elif token.startswith("ال") and len(token) > 2:
            marker_tokens += 1
    return OrthographyMetrics(
        letters=len(letters),
        arabic_only=sum(1 for ch in letters if ch in ARABIC_ORTHOGRAPHY_LETTERS),
        persian_specific=sum(1 for ch in letters if ch in PERSIAN_SPECIFIC_LETTERS),
        tokens=len(tokens),
        arabic_marker_tokens=marker_tokens,
    )


def normalize_letters(text: str) -> str:
    """Rewrite Arabic orthography as Persian letters (``ة`` untouched)."""
    return (text or "").translate(_TO_PERSIAN)


def normalize_structured_field(text: str) -> str:
    """Letter-only rewrite for stored names, places and event titles (Phase Y-8).

    The same mapping the headline/summary pass uses (ي→ی, ك→ک, ى→ی) is applied to
    fields that are saved verbatim, so one actor does not appear with Arabic letters
    in ``actors`` and Persian letters in the summary. Nothing else changes: Latin
    names and «ة» stay exactly as the model wrote them.
    """
    return normalize_letters(text)


def normalize_structured_list(values: list[str]) -> list[str]:
    """Apply :func:`normalize_structured_field` to every entry of a structured list."""
    return [normalize_structured_field(str(value)) for value in values or []]


def normalize_pair(headline: str, summary: str) -> tuple[str, str, dict]:
    """Decide for headline+summary together, then normalise both or neither.

    The decision is taken on the combined text so a short Persian headline and a
    long summary are judged as one unit, and the reported metrics explain exactly
    why normalisation did or did not happen.
    """
    combined = f"{headline or ''}\n{summary or ''}"
    metrics = measure(combined)
    if metrics.arabic_only == 0:
        reason = "no_arabic_orthography"
    elif metrics.persian_specific < MIN_PERSIAN_SPECIFIC_LETTERS:
        reason = "no_persian_specific_letters"
    elif metrics.arabic_marker_density > MAX_ARABIC_MARKER_DENSITY:
        reason = "too_many_arabic_markers"
    else:
        reason = "normalized"

    applied = reason == "normalized"
    new_headline = normalize_letters(headline) if applied else (headline or "")
    new_summary = normalize_letters(summary) if applied else (summary or "")
    return new_headline, new_summary, {
        "applied": applied,
        "reason": reason,
        "thresholds": {
            "max_arabic_marker_density": MAX_ARABIC_MARKER_DENSITY,
            "min_persian_specific_letters": MIN_PERSIAN_SPECIFIC_LETTERS,
        },
        "combined": metrics.as_dict(),
        "headline": measure(headline).as_dict(),
        "summary": measure(summary).as_dict(),
        "arabic_only_before": metrics.arabic_only,
        "arabic_only_after": measure(new_headline + "\n" + new_summary).arabic_only,
    }
