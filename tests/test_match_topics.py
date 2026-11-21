"""Direct tests for ``match_topics``: multi-topic and regional fallback.

Added before any change to the classification rules (ISS-036 audit baseline), so a
later fix has to keep these contracts or change them deliberately.
"""

from __future__ import annotations

import pytest

from app.editorial import topics as topics_module
from app.editorial.newsworthiness import non_political_story_reason
from app.editorial.topics import REGIONAL_TOPIC_KEY, build_topics, match_topics

PAYLOAD = {
    "topics": [
        {
            "key": "alpha",
            "name_fa": "آلفا",
            "priority": 1,
            "min_importance": 2,
            "countries": ["ایران"],
            "actors": ["وزارت خارجه"],
            "places": ["تهران"],
            "keywords": ["مذاکرات"],
        },
        {
            "key": "beta",
            "name_fa": "بتا",
            "priority": 2,
            "min_importance": 2,
            "countries": ["ایران"],
            "actors": ["سپاه"],
            "places": ["تهران"],
            "keywords": ["موشک"],
        },
    ]
}


@pytest.fixture()
def configured(monkeypatch):
    """Pin the topic list to a two-topic payload plus the regional fallback."""
    monkeypatch.setattr(topics_module, "load_topics", lambda: build_topics(PAYLOAD))
    return topics_module


def test_a_story_can_match_several_topics(configured):
    keys = [
        topic.key
        for topic in match_topics(
            ["ایران"], ["وزارت خارجه", "سپاه"], "تهران", "مذاکرات و موشک"
        )
    ]
    assert keys == ["alpha", "beta"]


def test_regional_topic_is_used_only_as_a_fallback(configured):
    keys = [
        topic.key
        for topic in match_topics(["یمن"], ["ارتش یمن"], "صنعا", "درگیری در صنعا")
    ]
    assert keys == [REGIONAL_TOPIC_KEY]


def test_regional_topic_is_not_added_next_to_a_specific_topic(configured):
    keys = [topic.key for topic in match_topics(["ایران"], ["سپاه"], "تهران", "پرتاب موشک")]
    assert "beta" in keys
    assert REGIONAL_TOPIC_KEY not in keys


def test_a_story_without_any_topic_is_other(configured):
    assert match_topics(["فرانسه"], ["ماکرون"], "پاریس", "نتیجه انتخابات") == []


def test_a_country_alone_does_not_make_a_topic(configured):
    """The primary-subject rule needs an actor or place to corroborate the country."""
    assert match_topics(["ایران"], ["بانک مرکزی"], None, "تورم") == []


def test_regional_topic_also_requires_corroboration(configured):
    assert match_topics(["یمن"], ["وزارت خارجه فرانسه"], None, "بیانیه") == []


def test_zwnj_and_spacing_variants_are_normalized(configured):
    keys = [topic.key for topic in match_topics([], [], "باب‌المندب", "حمله به کشتی")]
    assert keys == [REGIONAL_TOPIC_KEY]


# --- Phase A: anchors, importance floor and generic-actor leakage ---
# Regression set built from real ai_payload rows of the 2026-11-21 export.


def _keys(countries, actors, location, title, importance):
    return [
        topic.key
        for topic in match_topics(countries, actors, location, title, importance)
    ]


def test_gaza_strike_is_not_filed_under_iran():
    """A Gaza strike used to match ``ir`` through the generic actor «ارتش»."""
    keys = _keys(
        ["فلسطین", "اسرائیل"],
        ["ارتش اسرائیل", "حماس"],
        "غزه",
        "کشته شدن هفت فلسطینی در حملات پهپادی اسرائیل",
        4,
    )
    assert keys == ["il"]


def test_lebanon_shelling_is_not_filed_under_iran():
    keys = _keys(
        ["لبنان", "اسرائیل"],
        ["ارتش اسرائیل"],
        "لبنان",
        "گلوله‌باران توپخانه‌ای روستاهای لبنان توسط ارتش اسرائیل",
        4,
    )
    assert keys == ["il"]


def test_a_story_without_china_is_never_filed_under_us_china():
    """«آمریکا و چین» used to match any story datelined in Washington."""
    keys = _keys(
        ["ایران", "اسرائیل", "ایالات متحده آمریکا"],
        ["عباس عراقچی", "سفیر اسرائیل"],
        "واشنگتن",
        "واکنش مقام ایرانی به اظهارات سفیر اسرائیل در واشنگتن",
        2,
    )
    assert "us-cn" not in keys


def test_a_china_story_still_matches_us_china():
    keys = _keys(
        ["چین", "ایالات متحده آمریکا"],
        ["وزارت بازرگانی چین", "ترامپ"],
        "پکن",
        "تعرفه‌های جدید آمریکا بر تراشه‌های چین",
        3,
    )
    assert "us-cn" in keys


def test_topic_min_importance_is_enforced():
    """``us`` declares ``min_importance: 3``; an importance-2 story must not enter it."""
    keys = _keys(
        ["ایالات متحده آمریکا", "ایران"],
        ["دونالد ترامپ"],
        "واشنگتن",
        "ترامپ: برنامه هسته‌ای ایران در نشست سه‌جانبه بررسی شد",
        2,
    )
    assert "us" not in keys
    assert keys == ["ir"]


def test_low_importance_story_does_not_enter_a_topic():
    keys = _keys(
        ["قطر"],
        ["هواداران نپالی"],
        "دوحه",
        "حضور پرشور جامعه نپالی‌های قطر در مسابقات کریکت",
        1,
    )
    assert keys == []


def test_a_sudan_story_without_an_iranian_actor_stays_out():
    """«جنگ» used to pull any war story into ``ir``."""
    keys = _keys(
        ["سودان"],
        ["یونیسف", "نیروهای مسلح سودان"],
        "سودان",
        "تأثیر جنگ سودان بر محرومیت کودکان دارای معلولیت از تحصیل",
        3,
    )
    assert "ir" not in keys


# --- RC-17: sport, tourism and routine incidents are not briefed ---
# The first two rows are real ai_payload rows from the 2026-11-21 export that used
# to enter the regional briefing once their importance reached 2.


def test_a_regional_sport_result_gets_no_topic():
    keys = _keys(
        ["قطر"],
        ["فدراسیون کریکت قطر", "هواداران نپالی"],
        "دوحه",
        "حضور پرشور جامعه نپالی‌های قطر در مسابقات کریکت",
        2,
    )
    assert keys == []


def test_a_tourism_promotion_gets_no_topic():
    keys = _keys(
        ["عربستان سعودی"],
        ["خبرگزاری رسمی عربستان"],
        "العلا",
        "برگزاری برنامه‌های آموزشی و تفریحی برای کودکان در العلا",
        2,
    )
    assert keys == []


def test_a_routine_road_accident_gets_no_topic():
    keys = _keys(
        ["ایران"],
        ["پلیس راهور"],
        "تهران",
        "تصادف زنجیره‌ای در جاده تهران–قم با پنج مصدوم",
        2,
    )
    assert keys == []


def test_a_politically_loaded_sport_story_keeps_its_topic():
    """An arrest or a ban makes sport political, so the veto must win."""
    keys = _keys(
        ["ایران"],
        ["وزارت ورزش ایران", "دادگاه انقلاب"],
        "تهران",
        "بازداشت ورزشکار ایرانی پس از خودداری از مسابقه با اسرائیل",
        3,
    )
    assert "ir" in keys


def test_sport_outside_the_region_is_not_this_gate_s_business():
    assert non_political_story_reason("لیگ فوتبال", None, ["اسپانیا"], [], "مادرید") is None


def test_a_security_story_that_mentions_a_stadium_is_kept():
    assert non_political_story_reason(
        "حمله موشکی به ورزشگاه در تل‌آویو", None, ["اسرائیل"], ["ارتش اسرائیل"], "تل‌آویو"
    ) is None


def test_a_labour_story_that_mentions_a_hotel_is_kept():
    assert non_political_story_reason(
        "اعتصاب کارکنان هتل‌های تهران", None, ["ایران"], ["کارگران هتل"], "تهران"
    ) is None


def test_the_gate_reports_why_it_fired():
    assert non_political_story_reason("مسابقات فوتبال لیگ ایران", None, ["ایران"], [], "تهران") == "sport"
    assert non_political_story_reason("تور گردشگری دبی", None, ["امارات"], [], "دبی") == "tourism"
    assert non_political_story_reason("آتش‌سوزی در یک انبار", None, ["ایران"], [], "تهران") == "incident"
