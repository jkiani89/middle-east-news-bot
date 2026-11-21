"""Log-only watchlist for Arabic-flavoured words in Persian output (Phase Y-8).

The language guard rejects Arabic *orthography* (``ي ك ة ى``), but a word written
with Persian letters and lifted straight from Arabic vocabulary passes it — real
bulletins contained «مسرح عملیات», «مظله امنیتی», «تقویضی بر توافق» and
«تجهیزات پوششی». This module only counts those words and reports them in
``ai_payload['persian_lint']``; it never blocks an article, changes a status or
spends a model call. A week of findings is the evidence needed before the main
editorial prompt is touched.
"""

from __future__ import annotations

import re
from typing import Any

# Words observed in real bulletins that a copy editor would rewrite.
SUSPECT_WORDS: tuple[str, ...] = (
    "مسرح",
    "مظله",
    "تقویض",
    "انتقای",
    "سریال",
    "تهجیر",
    "سیاده",
    "رئیس حکومت",
    "شهید",
    "شهدا",
    # Phase C: Arabic place names / vocabulary seen in the 2026-11-21 export. The
    # deterministic replacements in ``glossary.canonicalize_approved_names`` already
    # rewrite these; the watchlist measures how often the model still writes them.
    "طهران",
    "واشنطن",
    "مجزره",
    "تصدید",
    "بحریه",
    "استعوا",
    "مسسیر",
    "اکشافات",
)

QUOTE_PATTERN = re.compile(r'[«"“\'](.*?)[»"”\']', re.DOTALL)

# Phase D: stale office titles for a serving president. Persian output called Trump
# «رئیس‌جمهور پیشین», «منتخب» and «رئیس‌جمهور» in three consecutive stories. Log-only,
# like the vocabulary watchlist: the prompt states the rule, this measures violations.
STALE_OFFICE_TITLES: tuple[str, ...] = (
    "رئیس‌جمهور پیشین",
    "رئیس جمهور پیشین",
    "رئیس‌جمهور منتخب",
    "رئیس جمهور منتخب",
)


def _stale_title_findings(headline: str, summary: str) -> list[dict[str, str]]:
    """Report a stale Trump title only when the text actually mentions Trump."""
    text = f"{headline or ''}\n{summary or ''}"
    if "ترامپ" not in text:
        return []
    return [
        {"actor": "دونالد ترامپ", "phrase": phrase}
        for phrase in STALE_OFFICE_TITLES
        if phrase in text
    ]


def _find_locations(word: str, headline: str, summary: str) -> list[str]:
    """Detect whether a watchlist word occurs in headline, quote, or general narrative."""
    locations: list[str] = []
    clean_h = headline or ""
    clean_s = summary or ""

    # 1. Check headline
    if word in clean_h:
        locations.append("headline")

    # 2. Check quotes across summary and headline
    quotes = QUOTE_PATTERN.findall(clean_h) + QUOTE_PATTERN.findall(clean_s)
    in_quote = any(word in q for q in quotes)
    if in_quote:
        locations.append("quote")

    # 3. Check narrative (summary body outside explicit quotes)
    summary_sans_quotes = QUOTE_PATTERN.sub(" ", clean_s)
    if word in summary_sans_quotes:
        locations.append("narrative")

    return locations


def lint_persian_output(headline: str, summary: str) -> dict[str, Any]:
    """Count watchlist words in Persian output and report locations; report only, never block."""
    haystack = f"{headline or ''}\n{summary or ''}"
    findings = []
    for word in SUSPECT_WORDS:
        if word in haystack:
            count = haystack.count(word)
            locs = _find_locations(word, headline, summary)
            findings.append({
                "word": word,
                "count": count,
                "locations": locs,
            })

    return {
        "mode": "log",
        "blocking": False,
        "watchlist": list(SUSPECT_WORDS),
        "findings": findings,
        "official_titles": _stale_title_findings(headline, summary),
    }
