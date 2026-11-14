"""Phase R-2: location-weighted similarity and the relaxed clustering threshold.

The Nasser-hospital scenario is the acceptance case: five agencies reporting the
same strike in the same town must land on one event record.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.database.models import Article, Event
from app.processors.event_matcher import EventMatcher, calculate_event_similarity


def test_default_merge_threshold_is_relaxed_to_060():
    matcher = EventMatcher()
    assert matcher.merge_threshold == 0.60
    assert matcher.follow_up_threshold == 0.80


def test_location_weight_exceeds_keywords_and_weights_still_sum_to_one():
    event = Event(
        id=1,
        title="Israeli strike hits area near Nasser Hospital in Khan Younis",
        event_type="MIL",
        countries=["Palestine"],
        actors=["Israeli military"],
        location="Khan Younis",
    )
    incoming = {
        "event_title": "Nasser Hospital strike in Khan Younis",
        "categories": ["MIL"],
        "countries": ["Palestine"],
        "actors": ["Israeli military"],
        "location": "Khan Younis",
    }

    sim = calculate_event_similarity(event, incoming)

    assert sim.weights_applied["location"] == 0.22
    assert sim.weights_applied["keywords"] == 0.08
    assert sim.weights_applied["location"] > sim.weights_applied["keywords"]
    assert sum(sim.weights_applied.values()) == pytest.approx(1.0)


def test_five_agency_reports_of_one_incident_all_clear_the_threshold():
    """Five differently worded agency reports about the same strike must all match."""
    event = Event(
        id=1,
        title="Israeli strike hits area near Nasser Hospital in Khan Younis",
        event_type="MIL",
        countries=["Palestine"],
        actors=["Israeli military"],
        location="Khan Younis",
    )
    agency_reports = [
        "Nasser Hospital strike in Khan Younis reported by medics",
        "Blast near Nasser Hospital rocks Khan Younis",
        "Khan Younis: strike damages Nasser Hospital compound",
        "Casualties reported after Nasser Hospital strike in Khan Younis",
        "Israeli military confirms strike near Nasser Hospital",
    ]

    for title in agency_reports:
        incoming = {
            "event_title": title,
            "categories": ["MIL"],
            "countries": ["Palestine"],
            "actors": ["Israeli military"],
            "location": "Khan Younis",
        }
        sim = calculate_event_similarity(event, incoming)
        assert sim.total_score >= 0.60, f"{title} scored {sim.total_score}"
        assert sim.location_score == 1.0


@pytest.mark.asyncio
async def test_same_place_report_merges_into_the_existing_event():
    now = datetime.now(UTC)
    existing_event = Event(
        id=77,
        title="Israeli strike hits area near Nasser Hospital in Khan Younis",
        event_type="MIL",
        countries=["Palestine"],
        actors=["Israeli military"],
        location="Khan Younis",
        first_seen_at=now - timedelta(hours=3),
        last_updated_at=now - timedelta(hours=3),
        importance=4,
        status="active",
    )

    session = AsyncMock()
    scalars_mock = MagicMock()
    scalars_mock.all.return_value = [existing_event]
    scalars_mock.first.return_value = None
    session.scalars.return_value = scalars_mock

    article = Article(id=501, source_id=9, title="Casualties reported after Nasser Hospital strike")
    article.collected_at = now

    result = await EventMatcher().match_or_create_event(
        session,
        article,
        {
            "event_title": "Casualties reported after Nasser Hospital strike in Khan Younis",
            "categories": ["MIL"],
            "countries": ["Palestine"],
            "actors": ["Israeli military"],
            "location": "Khan Younis",
            "importance": 4,
        },
        source_name="Reuters",
    )

    assert result.is_new_event is False
    assert result.match_type == "auto"
    assert result.event_id == 77
    assert result.confidence >= 0.60


def test_unrelated_events_in_the_same_category_stay_apart():
    """A lower threshold must not merge different incidents that share a category."""
    event = Event(
        id=5,
        title="Heavy rains flood Hodeidah refugee camp",
        event_type="HUM",
        countries=["Yemen"],
        actors=["Red Crescent"],
        location="Hodeidah",
    )
    incoming = {
        "event_title": "Aid convoy reaches Aleppo shelters",
        "categories": ["HUM"],
        "countries": ["Syria"],
        "actors": ["UNHCR"],
        "location": "Aleppo",
    }

    sim = calculate_event_similarity(event, incoming)

    assert sim.total_score < 0.60
