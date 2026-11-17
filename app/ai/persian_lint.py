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
)

QUOTE_PATTERN = re.compile(r'[«"“\'](.*?)[»"”\']', re.DOTALL)


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
    }
