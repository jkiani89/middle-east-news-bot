"""Content quality and information sufficiency classification.

Separates event importance from information completeness:
- `full_text`: Extensive or full-body article text (typically >= 400 chars with detailed coverage).
- `short_summary`: Short but substantive text (< 400 chars) that conveys concrete factual information
  (action, actors, outcome) without placeholder or empty filler sentences.
- `insufficient_content`: Extremely short text, headline-only, or text containing placeholder phrases
  (e.g., "details not mentioned", "this news addresses...", circular repetitions of the headline).

These statuses are independent from event importance (1-5) and verification confidence.
"""

from __future__ import annotations

import re
from typing import Any

SHORT_BODY_THRESHOLD_CHARS = 400
MIN_SUBSTANTIVE_CHARS = 40

# Patterns indicating placeholder, empty snippet, or absence of substance
EMPTY_FILLER_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"جزئیات\s+(?:بیشتری|تکمیلی)?\s*(?:درباره|در\s+مورد)?\s*.*?\s*ذکر\s+نشده", re.IGNORECASE),
    re.compile(r"جزئیات\s+(?:بیشتری|تکمیلی)?\s*در\s+دسترس\s+نیست", re.IGNORECASE),
    re.compile(r"اطلاعات\s+بیشتری\s+(?:منتشر|ذکر)\s+نشده", re.IGNORECASE),
    re.compile(r"این\s+خبر\s+به\s+وضعیت\s+.*?\s*می‌پرد?ازد", re.IGNORECASE),
    re.compile(r"این\s+خبر\s+به\s+بررسی\s+.*?\s*می‌پرد?ازد", re.IGNORECASE),
    re.compile(r"این\s+گزارش\s+به\s+موضوع\s+.*?\s*می‌پرد?ازد", re.IGNORECASE),
    re.compile(r"متن\s+کامل\s+خبر\s+در\s+دسترس\s+نیست", re.IGNORECASE),
    re.compile(r"خبرگزاری\s+\w+\s+جزئیاتی\s+ارائه\s+نکرده", re.IGNORECASE),
)

QUALITY_LABELS_FA: dict[str, str] = {
    "full_text": "متن کامل",
    "short_summary": "خلاصه کوتاه",
    "insufficient_content": "اطلاعات ناکافی",
}


def classify_content_quality(
    title: str | None,
    text: str | None,
    text_source: str | None = None,
) -> dict[str, Any]:
    """Classify article content quality into full_text, short_summary, or insufficient_content.

    Returns a dict with:
    - ``status``: "full_text" | "short_summary" | "insufficient_content"
    - ``label_fa``: Persian display label
    - ``char_count``: length of text
    - ``is_insufficient``: bool indicating if content lacks substantive details
    - ``reason``: diagnostic explanation
    """
    clean_title = (title or "").strip()
    clean_text = (text or "").strip()
    char_len = len(clean_text)

    # 1. Virtually empty text (< 40 chars)
    if char_len < MIN_SUBSTANTIVE_CHARS:
        return {
            "status": "insufficient_content",
            "label_fa": QUALITY_LABELS_FA["insufficient_content"],
            "char_count": char_len,
            "is_insufficient": True,
            "reason": "text_too_short_or_empty",
        }

    # 2. Check if text is just a verbatim copy of the headline without added substance
    normalized_title = "".join(clean_title.split())
    normalized_text = "".join(clean_text.split())
    if normalized_text == normalized_title or (
        normalized_title and normalized_text.startswith(normalized_title) and len(normalized_text) <= len(normalized_title) + 15
    ):
        return {
            "status": "insufficient_content",
            "label_fa": QUALITY_LABELS_FA["insufficient_content"],
            "char_count": char_len,
            "is_insufficient": True,
            "reason": "headline_mirror_no_substance",
        }

    # 3. Check for placeholder / filler patterns in short texts (< 400 chars)
    if char_len < SHORT_BODY_THRESHOLD_CHARS:
        for pat in EMPTY_FILLER_PATTERNS:
            if pat.search(clean_text):
                return {
                    "status": "insufficient_content",
                    "label_fa": QUALITY_LABELS_FA["insufficient_content"],
                    "char_count": char_len,
                    "is_insufficient": True,
                    "reason": "filler_or_missing_details",
                }

        # Short text, but carries genuine substantive information
        return {
            "status": "short_summary",
            "label_fa": QUALITY_LABELS_FA["short_summary"],
            "char_count": char_len,
            "is_insufficient": False,
            "reason": "substantive_short_body",
        }

    # 4. Text >= 400 chars
    # If text is >= 400 chars, it is classified as full_text (or short_summary if text_source is strictly summary)
    return {
        "status": "full_text",
        "label_fa": QUALITY_LABELS_FA["full_text"],
        "char_count": char_len,
        "is_insufficient": False,
        "reason": "full_body_length",
    }
