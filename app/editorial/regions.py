"""Editorial geography: which places make a story a Middle East story.

The AI relevance flag is deliberately not trusted on its own. Adding global
outlets (Foreign Affairs, NYT, WaPo, Euronews, Reuters) brought in stories about
the Americas, Europe and East Asia, so every analysis is additionally checked
against this list before it can reach the channel.
"""

from __future__ import annotations

import unicodedata

# Normalized (casefolded, Persian letters unified) country and territory names.
MIDDLE_EAST_COUNTRIES: frozenset[str] = frozenset(
    {
        # English
        "iran",
        "israel",
        "palestine",
        "palestinian territories",
        "palestinian authority",
        "gaza",
        "gaza strip",
        "west bank",
        "egypt",
        "saudi arabia",
        "united arab emirates",
        "uae",
        "qatar",
        "turkey",
        "turkiye",
        "türkiye",
        "iraq",
        "syria",
        "lebanon",
        "yemen",
        "jordan",
        "kuwait",
        "oman",
        "bahrain",
        # Persian
        "ایران",
        "اسرائیل",
        "فلسطین",
        "سرزمین‌های فلسطینی",
        "غزه",
        "نوار غزه",
        "کرانه باختری",
        "مصر",
        "عربستان",
        "عربستان سعودی",
        "امارات",
        "امارات متحده عربی",
        "قطر",
        "ترکیه",
        "عراق",
        "سوریه",
        "لبنان",
        "یمن",
        "اردن",
        "کویت",
        "عمان",
        "بحرین",
    }
)

# Strategic waterways that make a story regionally relevant on their own.
MIDDLE_EAST_WATERWAYS: frozenset[str] = frozenset(
    {
        "strait of hormuz",
        "hormuz",
        "bab el-mandeb",
        "bab al-mandab",
        "bab-el-mandeb",
        "red sea",
        "persian gulf",
        "arabian gulf",
        "gulf of aden",
        "suez canal",
        "gulf of oman",
        "تنگه هرمز",
        "هرمز",
        "باب‌المندب",
        "باب المندب",
        "دریای سرخ",
        "خلیج فارس",
        "خلیج عدن",
        "کانال سوئز",
        "دریای عمان",
    }
)

_PERSIAN_LETTER_MAP = str.maketrans({"ي": "ی", "ك": "ک", "ۀ": "ه", "ة": "ه"})


def normalize_place(value: str) -> str:
    """Normalize a country/location string for comparison (Persian-aware)."""
    text = unicodedata.normalize("NFKC", str(value)).translate(_PERSIAN_LETTER_MAP)
    text = text.replace("\u200c", " ").strip().casefold()
    return " ".join(text.split()).strip(".,;،؛:()[]«»\"'")


def is_middle_east_country(value: str) -> bool:
    """Whether a single country name is part of the Middle East set."""
    return normalize_place(value) in MIDDLE_EAST_COUNTRIES


def mentions_middle_east_waterway(value: str | None) -> bool:
    """Whether a location string mentions one of the strategic waterways."""
    if not value:
        return False
    normalized = normalize_place(value)
    return normalized in MIDDLE_EAST_WATERWAYS or any(
        waterway in normalized for waterway in MIDDLE_EAST_WATERWAYS
    )


def has_middle_east_nexus(countries: list[str] | None, location: str | None = None) -> bool:
    """True when the analysis places the story in the region we cover.

    Accepts a country in the Middle East set, or a location naming one of the
    strategic waterways. Anything else is out of scope for this channel.
    """
    for country in countries or []:
        if is_middle_east_country(country):
            return True
    if location and is_middle_east_country(location):
        return True
    return mentions_middle_east_waterway(location)
