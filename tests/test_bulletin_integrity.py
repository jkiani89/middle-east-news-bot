"""Tests for Phase Y-13: Event Integrity and Comparison in Bulletin.

Verifies:
1. is_live_digest detection for URLs and titles, excluding Arabic 'مباشر'.
2. Liveblog clamping to liveblog_max_importance (3), and isolation in EventMatcher.
3. Perspectives gate: requires shared non-country actor or city; excludes broad countries & waterways.
4. Canonicalization of approved names (Putin, Trump, Xi Jinping, Shenzhen, APEC) and idempotency.
5. Persian Lint watchlist includes suspect words in log-only mode.
6. Daily bulletin text & PDF formatting: Section 1 cap, section titles, minimum importance counter,
   Google News source formatting, deduplication, silent parties toggle, and regional filtering.
7. Verification that EDITORIAL_SYSTEM_PROMPT and PROMPT_VERSION remain untouched.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.ai.persian_lint import SUSPECT_WORDS, lint_persian_output
from app.ai.summarizer import EDITORIAL_SYSTEM_PROMPT, PROMPT_VERSION
from app.config import settings
from app.editorial.glossary import canonicalize_approved_names
from app.editorial.liveblog import is_live_digest
from app.editorial.perspectives import check_perspectives_gate
from app.processors.event_matcher import EventMatcher
from app.reports.daily_report import (
    _format_source_display,
    _item_sort_key,
    format_persian_daily_report,
    has_regional_presence,
)
from app.reports.pdf_report import _sources_text

# -----------------------------------------------------------------------------
# 1. Liveblog / Rolling Digest Detection
# -----------------------------------------------------------------------------


def test_is_live_digest_urls():
    # Real liveblog URL witnesses from Bulletin #2 and #3
    url2 = "/news/liveblog/2026/11/16/live-houthis-advance-in-yemen-as-tankers-hit-sea-mines-in-strait-of-hormuz"
    url3 = "https://www.aljazeera.net/news/liveblog/2026/11/17/live-marib-updates"
    assert is_live_digest(url2, "News Title") is True
    assert is_live_digest(url3, "عنوان خبر") is True

    # Other patterns: /live-blog/, /live/, slug live-*
    assert is_live_digest("https://bbc.com/news/live-blog-middle-east", "Title") is True
    assert is_live_digest("https://reuters.com/world/live/", "Title") is True
    assert is_live_digest("https://aljazeera.com/news/live-ceasefire-talks", "Title") is True

    # Standard article URL
    assert is_live_digest("https://reuters.com/world/middle-east/article-one-2026-11-17", "Title") is False


def test_is_live_digest_title_prefixes():
    assert is_live_digest("https://example.com/story", "LIVE: Middle East crisis escalates") is True
    assert is_live_digest("https://example.com/story", "LIVE | Marib updates") is True

    # Standard title
    assert is_live_digest("https://example.com/story", "Normal title without live prefix") is False


def test_is_live_digest_excludes_arabic_mobasher():
    # Arabic 'مباشر' must NOT trigger live digest detection due to high false positives
    url = "https://example.com/news/2026/11/17/story"
    title_mobasher = "پخش زنده مستقیم یا مذاکرات مباشر"
    assert is_live_digest(url, title_mobasher) is False


# -----------------------------------------------------------------------------
# 2. Liveblog Clamping and EventMatcher Isolation
# -----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_liveblog_event_matcher_isolation_and_cap():
    matcher = EventMatcher()
    now = datetime.now(UTC)

    session = AsyncMock()
    # 1. Incoming live digest does NOT merge into existing active event
    existing_normal = SimpleNamespace(
        id=77,
        title="Red Sea tanker attack",
        event_type="MIL",
        countries=["Yemen"],
        actors=["Houthi"],
        location="Red Sea",
        importance=4,
        first_seen_at=now,
    )
    scalars_mock = MagicMock()
    scalars_mock.all.return_value = [existing_normal]
    session.scalars.return_value = scalars_mock

    # Incoming live digest
    article = SimpleNamespace(
        id=501,
        source_id=9,
        title="LIVE: Middle East crisis escalates",
        original_url="https://aljazeera.com/news/liveblog/2026/11/17/live-updates",
        canonical_url="https://aljazeera.com/news/liveblog/2026/11/17/live-updates",
        collected_at=now,
    )
    analysis_data = {
        "event_title": "LIVE: Middle East crisis escalates",
        "categories": ["MIL"],
        "countries": ["Yemen"],
        "actors": ["Houthi"],
        "location": "Red Sea",
        "importance": 5,
        "article_kind": "live_digest",
    }
    result = await matcher.match_or_create_event(session, article, analysis_data, "Al Jazeera")
    assert result.is_new_event is True
    assert result.match_type == "initial"
    assert result.event.importance <= settings.liveblog_max_importance  # Capped to 3

    # 2. Incoming normal article does NOT merge into existing live digest event
    existing_digest = SimpleNamespace(
        id=88,
        title="Live coverage of conflicts",
        event_type="MIL",
        countries=["Yemen"],
        actors=["Houthi"],
        location="Red Sea",
        importance=3,
        first_seen_at=now,
    )
    scalars_mock.all.return_value = [existing_digest]
    # Return details_fa with is_live_digest=True for candidate 88
    session.scalar.return_value = {"is_live_digest": True}

    article_normal = SimpleNamespace(
        id=502,
        source_id=9,
        title="Houthi attack hits tanker",
        original_url="https://reuters.com/news/article-normal",
        canonical_url="https://reuters.com/news/article-normal",
        collected_at=now,
    )
    analysis_normal = {
        "event_title": "Houthi attack hits tanker",
        "categories": ["MIL"],
        "countries": ["Yemen"],
        "actors": ["Houthi"],
        "location": "Red Sea",
        "importance": 4,
    }
    result2 = await matcher.match_or_create_event(session, article_normal, analysis_normal, "Reuters")
    assert result2.is_new_event is True
    assert result2.match_type == "initial"
    assert result2.event.id != 88


# -----------------------------------------------------------------------------
# 3. Perspectives Gate
# -----------------------------------------------------------------------------


def test_perspectives_gate_rejection_yemen_false_merge():
    # Case witnessed in Yemen #13: 2 camps only share country "Yemen", but actors are disjoint
    camp_a = {
        "source_name": "Saba News Agency",
        "actors": ["وزارت کشاورزی", "خانواده شهدا"],
        "location": "صنعا",
    }
    camp_b = {
        "source_name": "Reuters",
        "actors": ["انصارالله", "سنتکام"],
        "location": "دریای سرخ",
    }
    passed, shared = check_perspectives_gate([camp_a, camp_b], min_shared_actors=1)
    # Yemen and Red Sea are excluded; actors have 0 overlap -> gate rejects
    assert passed is False
    assert len(shared) == 0


def test_perspectives_gate_rejection_global_countries():
    # Phase Y-13 Audit: Global countries (Japan, South Korea, Brazil, Germany, etc.)
    # must be excluded as qualifying entities so sharing a country does NOT pass the gate.
    for country_fa, country_en in [
        ("ژاپن", "Japan"),
        ("کره جنوبی", "South Korea"),
        ("برزیل", "Brazil"),
        ("آلمان", "Germany"),
        ("کانادا", "Canada"),
        ("استرالیا", "Australia"),
    ]:
        camp_a = {
            "source_name": "Source A",
            "actors": ["وزارت بازرگانی"],
            "location": country_fa,
        }
        camp_b = {
            "source_name": "Source B",
            "actors": ["شرکت‌های خصوصی"],
            "location": country_en,
        }
        passed, shared = check_perspectives_gate([camp_a, camp_b], min_shared_actors=1)
        assert passed is False, f"Country {country_fa}/{country_en} should not pass the gate as a shared location"
        assert len(shared) == 0


def test_perspectives_gate_passes_with_shared_global_city():
    # Specific non-country cities (e.g. Tokyo, Seoul, Brasilia) DO qualify if shared
    for city_fa in ["توکیو", "سئول", "برازیلیا"]:
        camp_a = {
            "source_name": "Source A",
            "actors": ["مقامات دولتی"],
            "location": city_fa,
        }
        camp_b = {
            "source_name": "Source B",
            "actors": ["سرمایه‌گذاران"],
            "location": city_fa,
        }
        passed, shared = check_perspectives_gate([camp_a, camp_b], min_shared_actors=1)
        assert passed is True, f"Specific city {city_fa} should pass perspectives gate"
        assert len(shared) >= 1


def test_perspectives_gate_one_camp_has_location_other_empty():
    # Case: Camp A has specific location 'مأرب', but Camp B has no location and no actors
    camp_a = {
        "source_name": "Source A",
        "actors": [],
        "location": "مأرب",
    }
    camp_b = {
        "source_name": "Source B",
        "actors": [],
        "location": "",
    }
    passed, shared = check_perspectives_gate([camp_a, camp_b], min_shared_actors=1)
    assert passed is False
    assert len(shared) == 0


def test_perspectives_gate_compound_locations():
    from app.editorial.perspectives import extract_non_country_entities

    # Compound location with city + country: country is stripped, city retained
    assert extract_non_country_entities({"location": "Tokyo, Japan"}) == {"tokyo"}
    assert extract_non_country_entities({"location": "توکیو، ژاپن"}) == {"توکیو"}
    assert extract_non_country_entities({"location": "Cairo, Egypt"}) == {"cairo"}
    assert extract_non_country_entities({"location": "قاهره / مصر"}) == {"قاهره"}
    assert extract_non_country_entities({"location": "مأرب، یمن"}) == {"مأرب"}

    # Hyphenated place names: hyphens inside words are NOT split into meaningless pieces
    assert extract_non_country_entities({"location": "Port-au-Prince, Haiti"}) == {"port-au-prince"}
    assert extract_non_country_entities({"location": "Port-au-Prince"}) == {"port-au-prince"}
    assert extract_non_country_entities({"location": "Tel-Aviv, Israel"}) == {"tel-aviv"}
    assert extract_non_country_entities({"location": "Beirut - Lebanon"}) == {"beirut"}
    assert extract_non_country_entities({"location": "بیروت - لبنان"}) == {"بیروت"}

    # Compound actors: country parts are stripped, non-state actors retained
    assert extract_non_country_entities({"actors": ["Iran and Houthis"]}) == {"houthis"}
    assert extract_non_country_entities({"actors": ["ایران و انصارالله"]}) == {"انصارالله"}
    assert extract_non_country_entities({"actors": ["Houthis and Hezbollah"]}) == {"houthis", "hezbollah"}
    assert extract_non_country_entities({"actors": ["انصارالله و حزب‌الله"]}) == {"انصارالله", "حزب الله"}

    # Compound actors or locations with only countries/waterways: all stripped -> set()
    assert extract_non_country_entities({"actors": ["Iran and Russia"]}) == set()
    assert extract_non_country_entities({"actors": ["ایران و روسیه"]}) == set()
    assert extract_non_country_entities({"location": "Japan and South Korea"}) == set()
    assert extract_non_country_entities({"location": "ژاپن و کره جنوبی"}) == set()
    assert extract_non_country_entities({"location": "Red Sea and Gulf of Aden"}) == set()
    assert extract_non_country_entities({"location": "دریای سرخ و خلیج عدن"}) == set()


def test_perspectives_gate_compound_matching_behavior():
    # 1. Matching city with different compound forms qualifies
    camp_tokyo_compound = {
        "source_name": "Source A",
        "actors": ["مقامات"],
        "location": "Tokyo, Japan",
    }
    camp_tokyo_city = {
        "source_name": "Source B",
        "actors": ["سرمایه‌گذاران"],
        "location": "Tokyo",
    }
    passed, shared = check_perspectives_gate([camp_tokyo_compound, camp_tokyo_city], min_shared_actors=1)
    assert passed is True
    assert "tokyo" in shared

    # 2. Different cities in same country: country is stripped, cities disjoint -> rejects
    camp_osaka = {
        "source_name": "Source B",
        "actors": ["سرمایه‌گذاران"],
        "location": "Osaka, Japan",
    }
    passed, shared = check_perspectives_gate([camp_tokyo_compound, camp_osaka], min_shared_actors=1)
    assert passed is False
    assert len(shared) == 0

    # 3. Compound countries only: both stripped -> rejects
    camp_compound_countries = {
        "source_name": "Source A",
        "actors": ["دیپلمات‌ها"],
        "location": "Japan and South Korea",
    }
    camp_single_country = {
        "source_name": "Source B",
        "actors": ["مقامات ارشد"],
        "location": "South Korea",
    }
    passed, shared = check_perspectives_gate([camp_compound_countries, camp_single_country], min_shared_actors=1)
    assert passed is False
    assert len(shared) == 0


class StrictZeroCallProvider:
    """A strict provider mock that raises AssertionError on ANY attribute access or method invocation.

    Guarantees 100% that no provider methods (e.g. generate_text, complete, etc.) are accessed or invoked.
    """

    def __getattr__(self, name: str):
        raise AssertionError(f"StrictZeroCallProvider violation: provider.{name} was accessed or invoked!")

    def __call__(self, *args, **kwargs):
        raise AssertionError("StrictZeroCallProvider violation: provider was invoked directly!")


@pytest.mark.asyncio
async def test_perspectives_gate_explicit_mock_zero_model_calls(caplog):
    """Explicitly verify that when the gate rejects, AI provider is never called (0 model calls)."""
    import logging

    from app.processors.pipeline import _refresh_event_perspectives

    summary = SimpleNamespace(details_fa={}, event_id=88)
    # Two camps covering different cities in Japan (Tokyo vs Osaka) without shared actors
    rows = [
        (21, "Title 1", "Body 1", "Reuters", {"actors": ["مسئولان"], "location": "Tokyo, Japan"}),
        (22, "Title 2", "Body 2", "TASS", {"actors": ["کارشناسان"], "location": "Osaka, Japan"}),
    ]
    session = SimpleNamespace(
        execute=AsyncMock(return_value=SimpleNamespace(all=lambda: rows)),
        scalars=AsyncMock(return_value=SimpleNamespace(first=lambda: summary)),
    )
    strict_ai_provider = StrictZeroCallProvider()

    with caplog.at_level(logging.INFO):
        res = await _refresh_event_perspectives(session, 88, "Event 88", strict_ai_provider)

    assert res is False
    assert "reason=disjoint_entities" in caplog.text


@pytest.mark.asyncio
async def test_perspectives_gate_rejection_insufficient_data(caplog):
    import logging

    from app.processors.pipeline import _refresh_event_perspectives

    summary = SimpleNamespace(details_fa={}, event_id=77)
    # Articles from 2 camps with NO actors or location data
    rows = [
        (11, "Headline A", "Text A", "Reuters", {}),
        (12, "Headline B", "Text B", "TASS", {}),
    ]
    session = SimpleNamespace(
        execute=AsyncMock(return_value=SimpleNamespace(all=lambda: rows)),
        scalars=AsyncMock(return_value=SimpleNamespace(first=lambda: summary)),
    )
    strict_ai_provider = StrictZeroCallProvider()

    with caplog.at_level(logging.INFO):
        res = await _refresh_event_perspectives(session, 77, "Test Event", strict_ai_provider)

    assert res is False
    assert "reason=insufficient_data" in caplog.text


def test_perspectives_gate_passes_with_shared_actor():
    camp_a = {
        "source_name": "Saba News Agency",
        "actors": ["انصارالله", "نیروهای مسلح یمن"],
        "location": "صنعا",
    }
    camp_b = {
        "source_name": "Reuters",
        "actors": ["انصارالله", "نیروی دریایی آمریکا"],
        "location": "دریای سرخ",
    }
    passed, shared = check_perspectives_gate([camp_a, camp_b], min_shared_actors=1)
    assert passed is True
    assert "انصارالله" in shared


def test_perspectives_gate_passes_with_shared_city():
    camp_a = {
        "source_name": "Saba News Agency",
        "actors": ["نیروهای محلی"],
        "location": "مأرب",
    }
    camp_b = {
        "source_name": "Al Arabiya",
        "actors": ["قبایل"],
        "location": "مأرب",
    }
    passed, shared = check_perspectives_gate([camp_a, camp_b], min_shared_actors=1)
    assert passed is True
    assert "مارب" in shared or "مأرب" in shared


def test_perspectives_gate_canonicalizes_approved_names():
    # Russian source uses Arabic 'بوتین' vs Western source 'ولادیمیر پوتین'
    camp_rus = {
        "source_name": "TASS",
        "actors": ["فلادیمیر بوتین"],
        "location": "مسکو",
    }
    camp_west = {
        "source_name": "Reuters",
        "actors": ["ولادیمیر پوتین"],
        "location": "مسکو",
    }
    passed, shared = check_perspectives_gate([camp_rus, camp_west], min_shared_actors=1)
    assert passed is True
    assert any("پوتین" in s for s in shared)


# -----------------------------------------------------------------------------
# 4. Canonicalization of Approved Names
# -----------------------------------------------------------------------------


def test_canonicalize_approved_names_entities():
    # Putin
    assert canonicalize_approved_names("دیدار با فلادیمیر بوتین در مسکو") == "دیدار با ولادیمیر پوتین در مسکو"
    assert canonicalize_approved_names("به گزارش بوتین") == "به گزارش پوتین"

    # Trump
    assert canonicalize_approved_names("اظهارات دونالد ترمب پیرامون توافق") == "اظهارات دونالد ترامپ پیرامون توافق"
    assert canonicalize_approved_names("سخنان ترمب در کاخ سفید") == "سخنان ترامپ در کاخ سفید"

    # Xi Jinping
    assert canonicalize_approved_names("دیدار شی جین پینگ با سران") == "دیدار شی جین‌پینگ با سران"

    # Shenzhen
    assert canonicalize_approved_names("نشست در شنجن چین") == "نشست در شنژن چین"

    # APEC
    assert (
        canonicalize_approved_names("مجمع همکاری اقتصادی آسیا-پاسفیک پایان یافت")
        == "مجمع همکاری اقتصادی آسیا-اقیانوسیه (اپک) پایان یافت"
    )


def test_canonicalize_approved_names_idempotent():
    sample = "دیدار فلادیمیر بوتین و دونالد ترمب و شی جین پینگ در شنجن و اجلاس همکاری‌های اقتصادی آسیا-اقیانوسیه"
    first_pass = canonicalize_approved_names(sample)
    second_pass = canonicalize_approved_names(first_pass)
    assert first_pass == second_pass


# -----------------------------------------------------------------------------
# 5. Persian Lint Watchlist
# -----------------------------------------------------------------------------


def test_persian_lint_watchlist_log_only():
    # Must include suspect words
    for word in ("سیاده", "رئیس حکومت", "تهجیر", "شهید", "شهدا"):
        assert word in SUSPECT_WORDS

    # Log-only mode: does NOT fail or raise; accurately counts and locates words
    headline = "بیانیه درباره سیاده کشور"
    summary = "بزرگداشت شهید و شهدا بدون تهجیر ساکنان انجام شد."
    result = lint_persian_output(headline, summary)
    assert result["mode"] == "log"
    assert result["blocking"] is False
    found_words = {f["word"] for f in result["findings"]}
    assert "سیاده" in found_words
    assert "شهید" in found_words
    assert "شهدا" in found_words
    assert "تهجیر" in found_words

    siada_finding = next(f for f in result["findings"] if f["word"] == "سیاده")
    assert "headline" in siada_finding["locations"]


# -----------------------------------------------------------------------------
# 6. Bulletin Display, Sections, Sorting, Filtering & Formatting
# -----------------------------------------------------------------------------


def test_bulletin_regional_presence_filtering():
    # Middle East event
    me_event = {
        "countries": ["Yemen"],
        "location": "Sanaa",
    }
    assert has_regional_presence(me_event) is True

    # Waterway event
    waterway_event = {
        "countries": [],
        "location": "تنگه هرمز",
    }
    assert has_regional_presence(waterway_event) is True

    # Non-regional event (e.g. Shenzhen/Putin meeting #4 without Middle East presence)
    non_me_event = {
        "countries": ["China", "Russia"],
        "location": "Shenzhen",
    }
    assert has_regional_presence(non_me_event) is False


def test_bulletin_item_sort_key():
    it1 = {"importance": 5, "total_sources_count": 3, "first_seen_at": "2026-11-17T10:00:00+00:00"}
    it2 = {"importance": 5, "total_sources_count": 1, "first_seen_at": "2026-11-17T12:00:00+00:00"}
    it3 = {"importance": 4, "total_sources_count": 5, "first_seen_at": "2026-11-17T12:00:00+00:00"}

    # Sort key: (imp, srcs, ts)
    assert _item_sort_key(it1) > _item_sort_key(it2)
    assert _item_sort_key(it2) > _item_sort_key(it3)


def test_bulletin_section1_capped_at_configured_max(monkeypatch):
    monkeypatch.setattr(settings, "bulletin_section1_max", 3)
    rep_date = date(2026, 11, 17)

    items = [
        {
            "event_id": i,
            "title": f"Strategic Event {i}",
            "headline_fa": f"تیتر رویداد راهبردی {i}",
            "summary_fa": f"خلاصه خبر شماره {i} به زبان فارسی و با جزئیات کامل.",
            "importance": 5,
            "countries": ["ایران"],
            "category_fa": "نظامی",
            "verification_status_fa": "تأییدشده",
            "primary_source": "ایرنا",
            "primary_url": f"https://irna.ir/{i}",
            "additional_sources": [],
            "first_seen_at": "2026-11-17T08:00:00+00:00",
        }
        for i in range(1, 6)
    ]

    report = format_persian_daily_report(rep_date, items, as_html=False)

    # Section 1 should have 3 items
    assert "بخش اول: اهم تحولات (اهمیت ۴ و ۵)" in report
    # Overflow (2 items) should appear in Section 2
    assert "بخش دوم: سایر تحولات" in report


def test_google_news_source_formatting():
    # Direct source
    res1 = _format_source_display("Reuters", "https://reuters.com/news", as_html=True)
    assert '<a href="https://reuters.com/news">Reuters</a>' in res1

    # Google News aggregated source
    res2 = _format_source_display("Al Jazeera", "https://news.google.com/rss/articles/123", as_html=True)
    assert "Al Jazeera (Google News)" in res2
    assert '<a href="https://news.google.com/rss/articles/123">' in res2


def test_pdf_sources_deduplication():
    item = {
        "primary_source": "Reuters",
        "primary_url": "https://reuters.com/article-1",
        "additional_sources": [
            {"source": "Reuters", "url": "https://reuters.com/article-2"},
            {"source": "TASS", "url": "https://tass.com/article-1"},
        ],
    }
    rendered = _sources_text(item)
    # Reuters should only appear once
    assert rendered.count("Reuters") == 1
    assert "TASS" in rendered


def test_live_digest_badge_in_report():
    rep_date = date(2026, 11, 17)
    item = {
        "event_id": 99,
        "title": "Liveblog update",
        "headline_fa": "تحولات زنده منطقه",
        "summary_fa": "خلاصه تحولات زنده در سراسر منطقه طی ساعت گذشته.",
        "importance": 3,
        "is_live_digest": True,
        "countries": ["یمن"],
        "category_fa": "نظامی",
        "verification_status_fa": "تأییدشده",
        "primary_source": "الجزیره",
        "primary_url": "https://aljazeera.com/liveblog",
        "additional_sources": [],
    }
    report = format_persian_daily_report(rep_date, [item], as_html=False)
    assert "نوع پوشش: 🔴 پوشش زنده (چندموضوعی)" in report


# -----------------------------------------------------------------------------
# 7. Unaltered Protocol Safeguards
# -----------------------------------------------------------------------------


def test_prompt_version_and_editorial_prompt_untouched():
    assert PROMPT_VERSION == "v2-e7572f94"
    assert "نقش تو دستیار تحلیل‌گر ارشد و بی‌طرف اخبار خاورمیانه" in EDITORIAL_SYSTEM_PROMPT
