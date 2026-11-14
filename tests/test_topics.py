"""Phase T-1: the topic taxonomy and the per-topic editorial gate.

The channel now covers five briefings instead of only the Middle East. These tests
pin the primary-subject rule: a story belongs to one topic, passing mentions stay
out, and the regional topic keeps the previously audited behaviour.
"""

from __future__ import annotations

import pytest

from app.database.models import Event
from app.editorial.regions import has_middle_east_nexus, is_primary_actor
from app.editorial.topics import (
    REGIONAL_TOPIC_KEY,
    load_topics,
    match_primary_topic,
    topic_by_key,
    topic_for_event,
)


def test_topics_load_in_priority_order_with_the_regional_topic_last():
    keys = [topic.key for topic in load_topics()]
    assert keys == ["ir", "ye", "ru-ua", "us", REGIONAL_TOPIC_KEY]
    assert topic_by_key("ye") is not None
    assert topic_by_key("ye").name_fa == "یمن و دریای سرخ"


@pytest.mark.parametrize(
    ("countries", "actors", "location", "title", "expected"),
    [
        (["ایران"], ["وزارت امور خارجه ایران"], "تهران", "دور جدید مذاکرات", "ir"),
        (["Yemen"], ["Houthi forces"], "Sanaa", "Red Sea shipping attack", "ye"),
        (["Ukraine", "Russia"], ["Kremlin"], "Kharkiv", "Drone strike on the power grid", "ru-ua"),
        (["ایالات متحده آمریکا"], ["کنگره"], None, "مناظره بودجه در کنگره", "us"),
        (["Palestine"], ["UNRWA"], "Gaza city", "Aid convoy enters Gaza", "me"),
    ],
)
def test_primary_topic_assignment(countries, actors, location, title, expected):
    topic = match_primary_topic(countries, actors, location, title)
    assert topic is not None
    assert topic.key == expected


def test_a_passing_mention_is_not_a_topic():
    """The old anti-passing-mention rule still holds for every topic."""
    topic = match_primary_topic(
        ["ایالات متحده آمریکا", "ایران"],
        ["وزارت خزانه‌داری ایالات متحده آمریکا", "اسکات بسنت"],
        None,
        "US Treasury sanctions on Iranian oil shipping",
    )
    assert topic is None


def test_ukraine_story_used_to_be_rejected_and_is_now_covered():
    countries, actors, location = ["Ukraine", "Russia"], ["Kremlin"], "Kharkiv"

    assert has_middle_east_nexus(countries, location) is False
    assert is_primary_actor(countries, actors, location) is False

    topic = match_primary_topic(countries, actors, location, "Drone strike on Kharkiv grid")
    assert topic is not None
    assert topic.key == "ru-ua"


def test_us_housing_market_story_stays_out():
    """A domestic market story is not a briefing topic, even with a US actor."""
    topic = match_primary_topic(
        ["ایالات متحده آمریکا"],
        ["فدرال رزرو"],
        "نیویورک",
        "قیمت مسکن در آمریکا افزایش یافت",
    )
    assert topic is None


def test_event_row_is_classified_from_its_analysis_fields():
    event = Event(
        id=1,
        title="Houthi naval attack on a tanker in the Red Sea",
        event_type="MAR",
        countries=["Yemen"],
        actors=["Houthi forces"],
        location="Red Sea",
    )
    topic = topic_for_event(event)
    assert topic is not None
    assert topic.key == "ye"
