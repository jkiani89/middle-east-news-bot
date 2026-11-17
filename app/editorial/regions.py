"""Editorial geography: which places make a story a Middle East story.

The AI relevance flag is deliberately not trusted on its own. Adding global
outlets (Foreign Affairs, NYT, WaPo, Euronews, Reuters) brought in stories about
the Americas, Europe and East Asia, so every analysis is additionally checked
against this list before it can reach the channel.
"""

from __future__ import annotations

import re
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

# Comprehensive catalog of non-Middle East sovereign states, territories, and global powers
# in both English and Persian (normalized).
WORLD_COUNTRIES: frozenset[str] = frozenset(
    {
        # East Asia & Pacific
        "japan", "south korea", "republic of korea", "north korea", "dprk", "china", "taiwan", "mongolia",
        "ژاپن", "کره جنوبی", "کره شمالی", "چین", "تایوان", "مغولستان",
        # Southeast Asia
        "vietnam", "thailand", "indonesia", "malaysia", "singapore", "philippines", "myanmar", "burma",
        "cambodia", "laos", "brunei", "east timor", "timor-leste",
        "ویتنام", "تایلند", "اندونزی", "مالزی", "سنگاپور", "فیلیپین", "میانمار", "برمه", "کامبوج", "لائوس", "برونئی", "تیمور شرقی",
        # South Asia
        "india", "pakistan", "bangladesh", "sri lanka", "nepal", "afghanistan", "maldives", "bhutan",
        "هند", "پاکستان", "بنگلادش", "سری لانکا", "سری‌لانکا", "سریلانکا", "نپال", "افغانستان", "مالدیو", "بوتان",
        # Central Asia & Caucasus
        "kazakhstan", "uzbekistan", "turkmenistan", "kyrgyzstan", "tajikistan", "azerbaijan", "armenia", "georgia",
        "قزاقستان", "ازبکستان", "ترکمنستان", "قرقیزستان", "تاجیکستان", "آذربایجان", "ارمنستان", "گرجستان",
        # Europe
        "russia", "russian federation", "ukraine", "belarus", "united kingdom", "uk", "britain", "great britain", "england",
        "france", "germany", "italy", "spain", "portugal", "netherlands", "holland", "belgium", "switzerland",
        "austria", "poland", "sweden", "norway", "finland", "denmark", "greece", "czech republic", "czechia",
        "hungary", "romania", "bulgaria", "serbia", "croatia", "ireland", "slovakia", "slovenia", "lithuania",
        "latvia", "estonia", "cyprus", "malta", "luxembourg", "iceland", "albania", "north macedonia", "bosnia",
        "bosnia and herzegovina", "montenegro", "kosovo", "moldova", "vatican", "monaco",
        "روسیه", "اوکراین", "بلاروس", "بریتانیا", "انگلیس", "انگلستان", "بریتانیای کبیر",
        "فرانسه", "آلمان", "ایتالیا", "اسپانیا", "پرتغال", "هلند", "بلژیک", "سوئیس", "اتریش",
        "لهستان", "سوئد", "نروژ", "فنلاند", "دانمارک", "یونان", "چک", "جمهوری چک", "مجارستان",
        "رومانی", "بلغارستان", "صربستان", "کرواسی", "ایرلند", "اسلواکی", "اسلوونی", "لیتوانی",
        "لتونی", "استونی", "قبرس", "مالت", "لوکزامبورگ", "ایسلند", "آلبانی", "مقدونیه شمالی",
        "بوسنی", "بوسنی و هرزگوین", "مونته‌نگرو", "کوزوو", "مولداوی", "واتیکان", "موناکو",
        # North America
        "united states", "united states of america", "usa", "us", "u.s.", "u.s.a.", "canada", "mexico",
        "آمریکا", "ایالات متحده", "ایالات متحده آمریکا", "کانادا", "مکزیک",
        # Latin America & Caribbean
        "brazil", "argentina", "colombia", "venezuela", "chile", "peru", "ecuador", "bolivia",
        "paraguay", "uruguay", "cuba", "haiti", "dominican republic", "panama", "costa rica",
        "guatemala", "honduras", "el salvador", "nicaragua", "jamaica",
        "برزیل", "آرژانتین", "کلمبیا", "ونزوئلا", "شیلی", "پرو", "اکوادور", "بولیوی",
        "پاراگوئه", "اروگوئه", "کوبا", "هائیتی", "جمهوری دومینیکن", "پاناما", "کاستاریکا",
        "گواتمالا", "هندوراس", "السالوادور", "نیکاراگوئه", "جامائیکا",
        # Africa
        "sudan", "south sudan", "libya", "tunisia", "algeria", "morocco", "ethiopia", "somalia",
        "djibouti", "eritrea", "kenya", "uganda", "tanzania", "rwanda", "congo", "drc", "nigeria",
        "niger", "chad", "mali", "burkina faso", "senegal", "ghana", "ivory coast", "cote d'ivoire",
        "south africa", "zimbabwe", "zambia", "angola", "mozambique", "madagascar", "cameroon",
        "gabon", "namibia", "botswana",
        "سودان", "سودان جنوبی", "لیبی", "تونس", "الجزایر", "مراکش", "مغرب", "اتیوپی", "سومالی",
        "جیبوتی", "اریتره", "کنیا", "اوگاندا", "تانزانیا", "رواندا", "کنگو", "جمهوری دموکراتیک کنگو",
        "نیجریه", "نیجر", "چاد", "مالی", "بورکینافاسو", "سنگال", "غنا", "ساحل عاج", "آفریقای جنوبی",
        "زیمبابوه", "زامبیا", "آنگولا", "موزامبیک", "ماداگاسکار", "کامرون", "گابن", "نامیبیا", "بوتسوانا",
        # Oceania
        "australia", "new zealand", "papua new guinea", "fiji",
        "استرالیا", "نیوزیلند", "پاپوآ گینه نو", "فیجی",
    }
)

# Major global and regional waterways/seas beyond the Middle East
GLOBAL_WATERWAYS: frozenset[str] = frozenset(
    {
        "black sea", "baltic sea", "caspian sea", "mediterranean", "mediterranean sea",
        "south china sea", "east china sea", "taiwan strait", "panama canal", "indian ocean",
        "pacific ocean", "atlantic ocean", "strait of malacca", "malacca strait", "gulf of mexico", "north sea",
        "دریای سیاه", "دریای بالتیک", "دریای خزر", "دریای مازندران", "دریای مدیترانه",
        "دریای چین جنوبی", "دریای چین شرقی", "تنگه تایوان", "کانال پاناما", "اقیانوس هند",
        "اقیانوس آرام", "اقیانوس اطلس", "تنگه مالاکا", "خلیج مکزیک", "دریای شمال",
    }
)

# Unified global sets
ALL_COUNTRIES: frozenset[str] = frozenset(
    MIDDLE_EAST_COUNTRIES | WORLD_COUNTRIES
)

ALL_WATERWAYS: frozenset[str] = frozenset(
    MIDDLE_EAST_WATERWAYS | GLOBAL_WATERWAYS
)

# Major cities of the region. Analyses usually name a city rather than a country,
# so a strict gate that only knew country names would reject Tehran- or Cairo-datelined
# stories that are entirely in scope.
MIDDLE_EAST_CITIES: frozenset[str] = frozenset(
    {
        "tehran",
        "jerusalem",
        "tel aviv",
        "gaza city",
        "ramallah",
        "cairo",
        "riyadh",
        "jeddah",
        "abu dhabi",
        "dubai",
        "doha",
        "ankara",
        "istanbul",
        "baghdad",
        "basra",
        "mosul",
        "damascus",
        "aleppo",
        "beirut",
        "sanaa",
        "aden",
        "amman",
        "kuwait city",
        "muscat",
        "manama",
        "تهران",
        "قدس",
        "اورشلیم",
        "تل‌آویو",
        "رام‌الله",
        "قاهره",
        "ریاض",
        "جده",
        "ابوظبی",
        "دبی",
        "دوحه",
        "آنکارا",
        "استانبول",
        "بغداد",
        "بصره",
        "موصل",
        "دمشق",
        "حلب",
        "بیروت",
        "صنعا",
        "عدن",
        "امان",
        "مسقط",
        "منامه",
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


def is_country(value: str | None) -> bool:
    """Whether a place name represents any sovereign country or major territory globally."""
    if not value:
        return False
    return normalize_place(value) in ALL_COUNTRIES


def mentions_middle_east_waterway(value: str | None) -> bool:
    """Whether a location string mentions one of the strategic waterways."""
    if not value:
        return False
    normalized = normalize_place(value)
    return normalized in MIDDLE_EAST_WATERWAYS or any(
        waterway in normalized for waterway in MIDDLE_EAST_WATERWAYS
    )


def is_waterway(value: str | None) -> bool:
    """Whether a place name represents a strategic or major global waterway/sea."""
    if not value:
        return False
    normalized = normalize_place(value)
    return normalized in ALL_WATERWAYS or any(
        w in normalized for w in ALL_WATERWAYS
    )


# Persian name for each English form, so an English `countries` entry can still be
# matched against Persian `actors` text (and the other way round).
MIDDLE_EAST_COUNTRY_ALIASES: dict[str, str] = {
    "iran": "ایران",
    "israel": "اسرائیل",
    "palestine": "فلسطین",
    "palestinian territories": "فلسطین",
    "palestinian authority": "فلسطین",
    "gaza": "غزه",
    "gaza strip": "غزه",
    "west bank": "کرانه باختری",
    "egypt": "مصر",
    "saudi arabia": "عربستان",
    "united arab emirates": "امارات",
    "uae": "امارات",
    "qatar": "قطر",
    "turkey": "ترکیه",
    "turkiye": "ترکیه",
    "türkiye": "ترکیه",
    "iraq": "عراق",
    "syria": "سوریه",
    "lebanon": "لبنان",
    "yemen": "یمن",
    "jordan": "اردن",
    "kuwait": "کویت",
    "oman": "عمان",
    "bahrain": "بحرین",
}


def _name_appears(name: str, haystack_normalized: str) -> bool:
    """Whether a normalized place name occurs in a normalized text.

    Latin names need word boundaries ("iran" must not match "Tirana"); Persian
    names are matched as substrings because Persian attaches suffixes with ZWNJ,
    which normalization turns into a space.
    """
    if name.isascii():
        # Word-start match so "iran" finds "Iranian" but not "Tirana".
        return re.search(rf"\b{re.escape(name)}", haystack_normalized) is not None
    return name in haystack_normalized


def mentions_middle_east_place(value: str | None) -> bool:
    """Whether a free-text value names a Middle East country, city or waterway."""
    if not value:
        return False
    normalized = normalize_place(value)
    if not normalized:
        return False
    for name in (*MIDDLE_EAST_COUNTRIES, *MIDDLE_EAST_CITIES, *MIDDLE_EAST_WATERWAYS):
        if _name_appears(name, normalized):
            return True
    return False


def is_primary_actor(
    countries: list[str] | None,
    actors: list[str] | None,
    location: str | None = None,
) -> bool:
    """Whether a Middle East state is a *party* to the story, not a passing mention.

    A story that merely lists a regional country among many is out of scope (e.g. an
    Africa-wide analysis that mentions Egypt once). It qualifies only when one of the
    analysed countries is regional and that same country is named among the primary
    actors, or when the event location itself is a regional place.
    """
    if mentions_middle_east_place(location):
        return True

    actor_text = normalize_place(" | ".join(actors or []))
    if not actor_text:
        return False
    for country in countries or []:
        if not is_middle_east_country(country):
            continue
        normalized = normalize_place(country)
        candidates = {normalized, normalize_place(MIDDLE_EAST_COUNTRY_ALIASES.get(normalized, ""))}
        if any(candidate and _name_appears(candidate, actor_text) for candidate in candidates):
            return True
    return False


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
