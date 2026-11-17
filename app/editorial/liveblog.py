"""Liveblog and multi-topic live digest detection (Phase Y-13).

A liveblog (rolling coverage / live digest) covers multiple fast-moving,
unrelated events across time. Blending it into a single-topic Event or
generating narrative perspectives on it produces incoherent summaries and
false contradictions.

Detection criteria:
- URL path contains `/liveblog/`, `live-blog`, or exact path segment `live`.
- URL slug starts with `live-`.
- English title starts with "LIVE:" or "LIVE |".
- Arabic word "مباشر" is intentionally NOT used as a criterion due to high
  false positives (many standard news reports use مباشر to describe live statements).
"""

from __future__ import annotations

import urllib.parse

LIVE_TITLE_PREFIXES: tuple[str, ...] = ("LIVE:", "LIVE |")


def is_live_digest(url: str | None, title: str | None) -> bool:
    """Return True if an article is a multi-topic rolling liveblog / live digest."""
    if url:
        parsed = urllib.parse.urlsplit(str(url))
        path = parsed.path.lower()
        if "/liveblog/" in path or "live-blog" in path:
            return True
        segments = [seg for seg in path.split("/") if seg]
        if any(seg == "live" for seg in segments):
            return True
        if segments and segments[-1].startswith("live-"):
            return True

    if title:
        clean_title = str(title).strip()
        clean_title_upper = clean_title.upper()
        if any(clean_title_upper.startswith(prefix) for prefix in LIVE_TITLE_PREFIXES):
            return True

    return False
