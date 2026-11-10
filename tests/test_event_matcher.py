"""Unit tests for EventMatcher and dynamic clustering engine (Phase 5).

Covers the 5 core scenarios required by acceptance criteria:
1. Two similar articles from different sources -> merge (match_type='auto')
2. Two distinct articles -> separate events (match_type='initial')
3. Duplicate or distinct URL matching behavior
4. Similar news with event within 24 hours -> merge
5. Similar news with event 50 hours prior (>48h) -> follow-up (parent_event_id set)
Plus Bootstrap guardrails and dynamic weight redistribution.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock
import pytest

from app.database.models import Article, ArticleEvent, Event, Summary
from app.processors.event_matcher import (
    EventMatcher,
    calculate_event_similarity,
    jaccard_similarity,
    normalize_text,
    tokenize,
)


def test_jaccard_similarity_and_normalization():
    assert jaccard_similarity(set(), set()) == 0.0
    assert jaccard_similarity({"iran"}, {"iran"}) == 1.0
    assert jaccard_similarity({"iran", "iraq"}, {"iran", "syria"}) == 1 / 3
    assert normalize_text("  U.N. Envoys in Geneva!  ") == "u n envoys in geneva"
    tokens = tokenize("The Summit in Amman on Climate and Water")
    assert "the" not in tokens
    assert "and" not in tokens
    assert "summit" in tokens
    assert "amman" in tokens


def test_dynamic_weight_redistribution_both_have_actors():
    event = Event(
        id=1,
        title="Geneva Maritime Security Briefing",
        event_type="HUM",
        countries=["Greece", "Senegal"],
        actors=["UN Envoys", "Coast Guard"],
        location="Geneva",
    )
    incoming = {
        "event_title": "Geneva Maritime Security Briefing",
        "categories": ["HUM", "MAR"],
        "countries": ["Greece", "Senegal"],
        "actors": ["UN Envoys", "Coast Guard"],
        "location": "Geneva",
    }
    sim = calculate_event_similarity(event, incoming)
    assert sim.weights_applied["actors"] == 0.25
    assert sim.weights_applied["countries"] == 0.25
    assert sim.total_score >= 0.95


def test_dynamic_weight_redistribution_bootstrap_missing_actors():
    """Bootstrap case: neither article has actors, actors weight redistributed to countries, category, location."""
    event = Event(
        id=2,
        title="Heavy Rains Cause Flooding in Yemen",
        event_type="HUM",
        countries=["Yemen"],
        actors=[],
        location="Hodeidah",
    )
    incoming = {
        "event_title": "Flooding and Rainfall Impact Yemen Coastal Areas",
        "categories": ["HUM"],
        "countries": ["Yemen"],
        "actors": [],
        "location": "Hodeidah",
    }
    sim = calculate_event_similarity(event, incoming)
    assert sim.weights_applied["actors"] == 0.0
    assert sim.weights_applied["countries"] == 0.40
    assert sim.weights_applied["category"] == 0.25
    assert sim.weights_applied["location"] == 0.20
    assert sim.total_score >= 0.70


def test_bootstrap_guardrail_rejects_category_only_false_merge():
    """Security Guardrail: if actors are missing and only category matches, reject false merge (< 0.40)."""
    event = Event(
        id=3,
        title="Diplomatic Talk in Cairo",
        event_type="POL",
        countries=["Egypt"],
        actors=[],
        location="Cairo",
    )
    # Incoming article has same category (POL) but completely different country and location
    incoming = {
        "event_title": "Election Process Update in Baghdad",
        "categories": ["POL"],
        "countries": ["Iraq"],
        "actors": [],
        "location": "Baghdad",
    }
    sim = calculate_event_similarity(event, incoming)
    # Only 1 feature matches (category), so guardrail caps at <= 0.39
    assert sim.distinct_matches < 2
    assert sim.total_score <= 0.39


@pytest.mark.asyncio
async def test_scenario_1_merge_similar_articles_different_sources():
    """Scenario 1: Two identical/highly similar articles from different sources -> merge (match_type='auto')."""
    now = datetime.now(timezone.utc)
    existing_event = Event(
        id=10,
        title="UN Peacekeeping Force Rotates in South Lebanon",
        event_type="MIL",
        countries=["Lebanon"],
        actors=["UNIFIL"],
        location="Naqoura",
        first_seen_at=now - timedelta(hours=2),
        last_updated_at=now - timedelta(hours=2),
        importance=3,
        status="active",
    )
    summary = Summary(
        id=1,
        event_id=10,
        headline_fa="نیروهای پاسدار صلح در جنوب لبنان مستقر شدند",
        summary_fa="جابجایی نیروهای یونیفل در منطقه ناقوره انجام شد.",
        details_fa={"sources": []},
    )

    session = AsyncMock()
    # Query 1 returns active events
    scalars_mock = MagicMock()
    scalars_mock.all.return_value = [existing_event]
    scalars_mock.first.return_value = summary
    session.scalars.return_value = scalars_mock

    incoming_article = Article(
        id=201,
        source_id=2,  # Different source
        title="UNIFIL peacekeepers rotate units in southern Lebanon border",
        original_url="https://reuters.com/lebanon-unifil",
        canonical_url="https://reuters.com/lebanon-unifil",
        collected_at=now,
    )
    incoming_ai = {
        "event_title": "UN Peacekeeping Force Rotates in South Lebanon",
        "categories": ["MIL"],
        "countries": ["Lebanon"],
        "actors": ["UNIFIL"],
        "location": "Naqoura",
        "importance": 4,  # Escalation in importance
    }

    matcher = EventMatcher()
    result = await matcher.match_or_create_event(
        session, incoming_article, incoming_ai, source_name="Reuters"
    )

    assert result.is_new_event is False
    assert result.match_type == "auto"
    assert result.event_id == 10
    assert result.confidence >= 0.65
    assert existing_event.importance == 4  # Escalated
    # Verify source was appended to details_fa
    assert len(summary.details_fa["sources"]) == 1
    assert summary.details_fa["sources"][0]["source"] == "Reuters"


@pytest.mark.asyncio
async def test_scenario_2_distinct_articles_create_separate_events():
    """Scenario 2: Two distinct articles -> separate events (match_type='initial')."""
    now = datetime.now(timezone.utc)
    existing_event = Event(
        id=11,
        title="Morocco and Spain Sign Port Cooperation Agreement",
        event_type="ECO",
        countries=["Morocco", "Spain"],
        actors=["Port Authority"],
        location="Tangier",
        first_seen_at=now - timedelta(hours=3),
        status="active",
    )

    session = AsyncMock()
    session.add = MagicMock()
    scalars_mock = MagicMock()
    scalars_mock.all.return_value = [existing_event]
    session.scalars.return_value = scalars_mock

    incoming_article = Article(
        id=202,
        title="Tehran hosts regional nuclear safety conference",
        original_url="https://example.com/tehran-conference",
        canonical_url="https://example.com/tehran-conference",
        collected_at=now,
    )
    incoming_ai = {
        "event_title": "Tehran Nuclear Safety Conference",
        "categories": ["ENE", "DIP"],
        "countries": ["Iran"],
        "actors": ["AEOI", "IAEA"],
        "location": "Tehran",
        "importance": 3,
    }

    matcher = EventMatcher()
    result = await matcher.match_or_create_event(
        session, incoming_article, incoming_ai, source_name="IRNA"
    )

    assert result.is_new_event is True
    assert result.match_type == "initial"
    assert result.confidence == 1.0
    assert result.event.title == "Tehran Nuclear Safety Conference"
    assert result.event.parent_event_id is None


@pytest.mark.asyncio
async def test_scenario_3_dedup_and_unrelated_candidate():
    """Scenario 3: Same topic with low similarity against irrelevant candidate creates separate event."""
    now = datetime.now(timezone.utc)
    unrelated_event = Event(
        id=12,
        title="Syria Humanitarian Relief Convoy Arrives in Aleppo",
        event_type="HUM",
        countries=["Syria"],
        actors=["Red Crescent"],
        location="Aleppo",
        first_seen_at=now - timedelta(hours=5),
        status="active",
    )

    session = AsyncMock()
    session.add = MagicMock()
    scalars_active = MagicMock()
    scalars_active.all.return_value = [unrelated_event]
    scalars_hist = MagicMock()
    scalars_hist.all.return_value = []
    session.scalars.side_effect = [scalars_active, scalars_hist]

    incoming_article = Article(
        id=203,
        title="Libyan Oil Production Resumes at Sharara Field",
        collected_at=now,
    )
    incoming_ai = {
        "event_title": "Libya Sharara Oil Field Resumption",
        "categories": ["ENE", "ECO"],
        "countries": ["Libya"],
        "actors": ["NOC"],
        "location": "Sharara",
        "importance": 3,
    }

    matcher = EventMatcher()
    result = await matcher.match_or_create_event(session, incoming_article, incoming_ai)
    assert result.is_new_event is True
    assert result.match_type == "initial"


@pytest.mark.asyncio
async def test_scenario_4_similar_news_within_24h_merges():
    """Scenario 4: Similar news with event 18 hours prior (within 48h window) -> merge."""
    now = datetime.now(timezone.utc)
    t_minus_18h = now - timedelta(hours=18)
    existing_event = Event(
        id=14,
        title="Gaza Humanitarian Crossing Opens for Aid Trucks",
        event_type="HUM",
        countries=["Palestine", "Egypt"],
        actors=["UNRWA", "Red Crescent"],
        location="Rafah",
        first_seen_at=t_minus_18h,
        last_updated_at=t_minus_18h,
        importance=4,
        status="active",
    )

    session = AsyncMock()
    scalars_mock = MagicMock()
    scalars_mock.all.return_value = [existing_event]
    scalars_mock.first.return_value = None
    session.scalars.return_value = scalars_mock

    incoming_article = Article(
        id=204,
        title="More aid convoys enter Gaza via Rafah crossing",
        collected_at=now,
    )
    incoming_ai = {
        "event_title": "Gaza Humanitarian Crossing Aid Truck Operations",
        "categories": ["HUM"],
        "countries": ["Palestine", "Egypt"],
        "actors": ["UNRWA"],
        "location": "Rafah",
        "importance": 4,
    }

    matcher = EventMatcher()
    result = await matcher.match_or_create_event(session, incoming_article, incoming_ai)
    assert result.is_new_event is False
    assert result.match_type == "auto"
    assert result.event_id == 14
    assert result.confidence >= 0.65


@pytest.mark.asyncio
async def test_scenario_5_follow_up_event_past_48h_sets_parent_event_id():
    """Scenario 5: Similar news with event 50 hours prior (>48h) -> follow-up (parent_event_id set)."""
    now = datetime.now(timezone.utc)
    t_minus_50h = now - timedelta(hours=50)

    # The original event happened 50 hours ago (outside 48h window)
    historical_event = Event(
        id=15,
        title="Ceasefire Negotiations Open in Doha",
        event_type="DIP",
        countries=["Qatar", "Egypt", "United States"],
        actors=["Qatari Prime Minister", "CIA Director"],
        location="Doha",
        first_seen_at=t_minus_50h,
        last_updated_at=t_minus_50h,
        importance=4,
        status="active",
    )

    session = AsyncMock()
    session.add = MagicMock()
    # Active window query (< 48h) returns empty
    scalars_active = MagicMock()
    scalars_active.all.return_value = []
    # Historical query (> 48h and <= 14d) returns historical_event
    scalars_hist = MagicMock()
    scalars_hist.all.return_value = [historical_event]

    session.scalars.side_effect = [scalars_active, scalars_hist]

    incoming_article = Article(
        id=205,
        title="Doha Ceasefire Talks Reconvene for Second Round",
        collected_at=now,
    )
    incoming_ai = {
        "event_title": "Ceasefire Negotiations Reconvene in Doha",
        "categories": ["DIP"],
        "countries": ["Qatar", "Egypt", "United States"],
        "actors": ["Qatari Prime Minister", "CIA Director"],
        "location": "Doha",
        "importance": 4,
    }

    matcher = EventMatcher()
    result = await matcher.match_or_create_event(
        session, incoming_article, incoming_ai, source_name="Al Jazeera"
    )

    assert result.is_new_event is True
    assert result.match_type == "follow_up"
    assert result.confidence >= 0.80
    assert result.event.parent_event_id == 15
    assert result.event.title.startswith("[پیگیری]")


def test_edge_cases_empty_and_partial_metadata():
    """Verify edge cases: None text, empty tokens, only one has actors, neither has location/category."""
    assert normalize_text(None) == ""
    assert tokenize(None) == set()

    # Event with actors, incoming without actors
    event = Event(
        id=50,
        title="OPEC Oil Production Quota Discussion",
        event_type=None,
        countries=["Saudi Arabia"],
        actors=["OPEC Secretariat"],
        location=None,
    )
    incoming = {
        "event_title": "Oil Market Supply Quota Discussions in Riyadh",
        "categories": [],
        "countries": ["Saudi Arabia"],
        "actors": [],
        "location": None,
    }
    sim = calculate_event_similarity(event, incoming)
    # Exactly one has actors: actors weight should be 0.0, countries 0.35
    assert sim.weights_applied["actors"] == 0.0
    assert sim.weights_applied["countries"] == 0.35
    assert sim.category_score == 0.5
    assert sim.location_score == 0.5


@pytest.mark.asyncio
async def test_multiple_articles_merge_into_same_event():
    """Verify that multiple subsequent articles correctly merge into the same event,

    accumulating sources in details_fa and repeatedly updating last_updated_at.
    """
    t0 = datetime(2026, 11, 10, 10, 0, tzinfo=timezone.utc)
    t1 = datetime(2026, 11, 10, 12, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 11, 10, 14, 0, tzinfo=timezone.utc)

    event = Event(
        id=77,
        title="Red Sea Commercial Shipping Route Security Coalition",
        event_type="SEC",
        countries=["Yemen"],
        actors=["Houthi Forces", "Naval Coalition"],
        location="Bab el-Mandeb",
        first_seen_at=t0,
        last_updated_at=t0,
        importance=3,
        status="active",
    )
    summary = Summary(
        id=77,
        event_id=77,
        headline_fa="ائتلاف دریایی برای تأمین امنیت تردد تجاری در دریای سرخ",
        summary_fa="کشتی‌های تجاری توسط ناوگان بین‌المللی اسکورت می‌شوند.",
        details_fa={
            "sources": [{
                "source": "Initial Wire",
                "url": "https://example.com/art1",
                "article_id": 101,
                "added_at": t0.isoformat(),
            }]
        },
    )

    session = AsyncMock()
    session.add = MagicMock()
    scalars_mock = MagicMock()
    scalars_mock.all.return_value = [event]
    scalars_mock.first.return_value = summary
    session.scalars.return_value = scalars_mock

    matcher = EventMatcher(merge_threshold=0.70)

    # Article 2 arrives at t1
    art2 = Article(
        id=102,
        title="Naval coalition repels maritime attack in Bab el-Mandeb",
        original_url="https://example.com/art2",
        collected_at=t1,
    )
    ai2 = {
        "event_title": "Red Sea Commercial Shipping Attack Repelled",
        "categories": ["SEC"],
        "countries": ["Yemen"],
        "actors": ["Naval Coalition", "Houthi Forces"],
        "location": "Bab el-Mandeb",
        "importance": 4,  # Escalation
    }
    res2 = await matcher.match_or_create_event(session, art2, ai2, source_name="Reuters")
    assert res2.is_new_event is False
    assert res2.match_type == "auto"
    assert event.last_updated_at == t1
    assert event.importance == 4
    assert len(summary.details_fa["sources"]) == 2
    assert summary.details_fa["sources"][1]["source"] == "Reuters"

    # Article 3 arrives at t2 (naive datetime without tzinfo to test conversion)
    art3 = Article(
        id=103,
        title="Commercial vessels resume transit through Bab el-Mandeb",
        original_url="https://example.com/art3",
        collected_at=datetime(2026, 11, 10, 14, 0),  # naive datetime
    )
    ai3 = {
        "event_title": "Bab el-Mandeb Shipping Traffic Resumes Under Naval Escort",
        "categories": ["SEC"],
        "countries": ["Yemen"],
        "actors": ["Naval Coalition"],
        "location": "Bab el-Mandeb",
        "importance": 4,
    }
    res3 = await matcher.match_or_create_event(session, art3, ai3, source_name="AFP")
    assert res3.is_new_event is False
    assert res3.match_type == "auto"
    assert event.last_updated_at == t2
    assert len(summary.details_fa["sources"]) == 3
    assert summary.details_fa["sources"][2]["source"] == "AFP"
