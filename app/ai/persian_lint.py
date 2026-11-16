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

from typing import Any

# Words observed in real bulletins that a copy editor would rewrite.
SUSPECT_WORDS: tuple[str, ...] = (
    "مسرح",
    "مظله",
    "تقویض",
    "انتقای",
    "سریال",
    "تهجیر",
)


def lint_persian_output(headline: str, summary: str) -> dict[str, Any]:
    """Count watchlist words in one Persian output; report only, never block."""
    haystack = f"{headline or ''}\n{summary or ''}"
    findings = [
        {"word": word, "count": haystack.count(word)}
        for word in SUSPECT_WORDS
        if word in haystack
    ]
    return {
        "mode": "log",
        "blocking": False,
        "watchlist": list(SUSPECT_WORDS),
        "findings": findings,
    }
