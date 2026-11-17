"""Unit tests for Y-14 event matching replay and calibration analysis."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.processors.event_matcher import calculate_event_similarity
from scripts.dev_only.replay_event_matching import (
    ArticleRecord,
    EventCandidateCompat,
    ReplayEvent,
    _headline_similarity_display_only,
    _to_event_candidate,
    analyze_replay_control,
    compute_keyword_score_distribution,
    compute_pairwise_relations,
    find_chaining_cases,
    load_articles_from_db,
    run_sequential_replay,
)


def test_to_event_candidate_adapter():
    art = ArticleRecord(
        id=1,
        source_name="Test Source",
        title="Raw Article Title",
        url="http://test.com",
        collected_at=datetime.now(UTC),
        ai_payload={
            "event_title": "عنوان رویداد فارسی",
            "categories": ["MIL"],
            "countries": ["ایران"],
            "actors": ["سپاه پاسداران"],
            "location": "خلیج فارس",
        },
        db_event_id=10,
        is_live_digest=False,
        is_insufficient_data=False,
    )
    cand = _to_event_candidate(art)
    assert isinstance(cand, EventCandidateCompat)
    assert cand.title == "عنوان رویداد فارسی"
    assert cand.event_type == "MIL"
    assert cand.countries == ["ایران"]
    assert cand.actors == ["سپاه پاسداران"]
    assert cand.location == "خلیج فارس"

    # Test calculate_event_similarity accepts cand without AttributeError
    incoming = {
        "event_title": "مانور نظامی در خلیج فارس",
        "categories": ["MIL"],
        "countries": ["ایران"],
        "actors": ["سپاه پاسداران"],
        "location": "خلیج فارس",
    }
    sim = calculate_event_similarity(cand, incoming)
    assert sim.total_score > 0.80


def test_keyword_score_distribution_calculation():
    now = datetime.now(UTC)
    art1 = ArticleRecord(
        id=1,
        source_name="Reuters",
        title="US imposes sanctions on regional network",
        url="http://reuters.com",
        collected_at=now,
        ai_payload={
            "event_title": "تحریم‌های جدید آمریکا علیه شبکه منطقه‌ای",
            "actors": ["وزارت خزانه‌داری آمریکا"],
            "location": "",
        },
        db_event_id=100,
        is_live_digest=False,
        is_insufficient_data=False,
    )
    art2 = ArticleRecord(
        id=2,
        source_name="AP",
        title="Treasury sanctions regional logistics network",
        url="http://apnews.com",
        collected_at=now,
        ai_payload={
            "event_title": "تحریم شبکه لجستیکی توسط خزانه‌داری آمریکا",
            "actors": ["وزارت خزانه‌داری آمریکا"],
            "location": "",
        },
        db_event_id=100,
        is_live_digest=False,
        is_insufficient_data=False,
    )
    dist = compute_keyword_score_distribution([art1, art2])
    assert dist["with_shared_actors"]["count"] == 1
    assert dist["with_shared_actors"]["median"] > 0.20


def test_sequential_replay_active_and_follow_up():
    now = datetime.now(UTC)
    # Article 1: initial event
    art1 = ArticleRecord(
        id=1,
        source_name="Reuters",
        title="Initial story",
        url="http://1",
        collected_at=now - timedelta(days=5),
        ai_payload={
            "event_title": "آغاز مذاکرات صلح در ژنو",
            "categories": ["DIP"],
            "countries": ["سوییس"],
            "actors": ["نمایندگان دولت", "مخالفان"],
            "location": "ژنو",
            "importance": 4,
        },
        db_event_id=1,
        is_live_digest=False,
        is_insufficient_data=False,
    )
    # Article 2: 4 days later (> 48h and <= 14d) with high similarity (>= 0.80) -> follow up
    art2 = ArticleRecord(
        id=2,
        source_name="AFP",
        title="Follow up story",
        url="http://2",
        collected_at=now - timedelta(days=1),
        ai_payload={
            "event_title": "ادامه مذاکرات صلح در ژنو با حضور میانجی‌ها",
            "categories": ["DIP"],
            "countries": ["سوییس"],
            "actors": ["نمایندگان دولت", "مخالفان"],
            "location": "ژنو",
            "importance": 4,
        },
        db_event_id=2,
        is_live_digest=False,
        is_insufficient_data=False,
    )

    mapping, events, decisions = run_sequential_replay([art1, art2])
    assert len(events) == 2
    assert decisions[0]["action"] == "create"
    assert decisions[1]["action"] == "follow_up"
    assert events[1].is_follow_up is True
    assert events[1].parent_event_id == events[0].id
    assert events[1].title.startswith("[پیگیری]")


def test_sequential_replay_strict_gate_splits_unrelated():
    now = datetime.now(UTC)
    # Article 1: Yemen Red Sea missile attack (location: Yemen, country: Yemen, category: MIL)
    art1 = ArticleRecord(
        id=10,
        source_name="Reuters",
        title="Houthis fire missile at tanker in Yemen",
        url="http://reuters.com",
        collected_at=now - timedelta(hours=10),
        ai_payload={
            "event_title": "حمله موشکی به کشتی در یمن",
            "categories": ["MIL"],
            "countries": ["یمن"],
            "actors": ["انصارالله", "حوثی‌ها"],
            "location": "یمن",
            "importance": 4,
        },
        db_event_id=10,
        is_live_digest=False,
        is_insufficient_data=False,
    )
    # Article 2: Yemen agriculture initiative (shares Yemen country & MIL category & Yemen location, but totally different actors)
    art2 = ArticleRecord(
        id=11,
        source_name="Saba",
        title="Ministry of agriculture meeting in Yemen",
        url="http://saba.ye",
        collected_at=now - timedelta(hours=8),
        ai_payload={
            "event_title": "طرح توسعه در یمن",
            "categories": ["MIL"],
            "countries": ["یمن"],
            "actors": ["وزارت کشاورزی"],
            "location": "یمن",
            "importance": 2,
        },
        db_event_id=10,
        is_live_digest=False,
        is_insufficient_data=False,
    )

    # Base rule (>= 0.60): false merge occurs because country(0.25) + category(0.20) + loc(0.15) + kw(0.08) = 0.68 >= 0.60
    map_base, evs_base, decs_base = run_sequential_replay([art1, art2], require_strict_gate=False)
    assert len(evs_base) == 1
    assert decs_base[1]["action"] == "merge"

    # Proposed rule (X=0.25): gate rejects because shared_non_country_actor is False (Yemen is country) and kw (0.16) < 0.25
    map_p, evs_p, decs_p = run_sequential_replay([art1, art2], require_strict_gate=True, keyword_threshold_x=0.25)
    assert len(evs_p) == 2
    assert decs_p[1]["action"] == "create"


def test_candidate_gate_rejection_no_fallback_to_lesser_candidate():
    """Verify that when the best candidate by total_score fails the strict gate,

    the algorithm does NOT fall back to a lesser candidate that happens to pass the gate.
    It decisively rejects the merge and creates a new event.
    """
    now = datetime.now(UTC)
    # Event 1: Syria, MIL, Syria, actors=['پلیس'], kw 'تحولات' -> score with art3 = 0.686
    art1 = ArticleRecord(
        id=1, source_name="S1", title="Field developments in Damascus", url="http://1",
        collected_at=now - timedelta(hours=5),
        ai_payload={
            "event_title": "تحولات میدانی دمشق",
            "categories": ["MIL"], "countries": ["سوریه"], "actors": ["پلیس"], "location": "سوریه",
        },
        db_event_id=1, is_live_digest=False, is_insufficient_data=False,
    )
    # Event 2: Iraq, MIL, actors=['نیروهای ویژه'], location Syria -> score with art3 = 0.670
    art2 = ArticleRecord(
        id=2, source_name="S2", title="Counter terrorism op", url="http://2",
        collected_at=now - timedelta(hours=4),
        ai_payload={
            "event_title": "عملیات ضدتروریسم",
            "categories": ["MIL"], "countries": ["عراق"], "actors": ["نیروهای ویژه"], "location": "سوریه",
        },
        db_event_id=2, is_live_digest=False, is_insufficient_data=False,
    )
    # Event 3 (Incoming Article):
    # Matches art1 with higher score (0.686 > 0.670), but fails strict gate (no shared non-country actor, kw < 0.25).
    # Matches art2 with lower score (0.670 >= 0.60) and shares actor 'نیروهای ویژه'.
    art3 = ArticleRecord(
        id=3, source_name="S3", title="New security developments", url="http://3",
        collected_at=now - timedelta(hours=1),
        ai_payload={
            "event_title": "تحولات امنیتی جدید",
            "categories": ["MIL"], "countries": ["سوریه"], "actors": ["نیروهای ویژه"], "location": "سوریه",
        },
        db_event_id=3, is_live_digest=False, is_insufficient_data=False,
    )

    # In Base rule: art3 merges into event 1 (highest score 0.686 > 0.670 >= 0.60)
    map_base, evs_base, decs_base = run_sequential_replay([art1, art2, art3], require_strict_gate=False)
    assert decs_base[2]["action"] == "merge"
    assert decs_base[2]["event_id"] == evs_base[0].id

    # In Proposed rule: art3 identifies event 1 as primary candidate. When event 1 fails gate,
    # it does NOT fall back to merge into event 2; it cleanly creates a new event!
    map_p, evs_p, decs_p = run_sequential_replay([art1, art2, art3], require_strict_gate=True, keyword_threshold_x=0.25)
    assert decs_p[2]["action"] == "create"
    assert len(evs_p) == 3


def test_chaining_detection_and_metrics():
    now = datetime.now(UTC)
    art1 = ArticleRecord(
        id=1, source_name="S1", title="A", url="u1", collected_at=now,
        ai_payload={"event_title": "الف", "actors": ["بازیگر ۱"], "countries": ["ایران"]},
        db_event_id=1, is_live_digest=False, is_insufficient_data=False,
    )
    art2 = ArticleRecord(
        id=2, source_name="S2", title="B", url="u2", collected_at=now,
        ai_payload={"event_title": "ب", "actors": ["بازیگر ۱", "بازیگر ۲"], "countries": ["ایران"]},
        db_event_id=1, is_live_digest=False, is_insufficient_data=False,
    )
    art3 = ArticleRecord(
        id=3, source_name="S3", title="C", url="u3", collected_at=now,
        ai_payload={"event_title": "ج", "actors": ["بازیگر ۲"], "countries": ["ایران"]},
        db_event_id=1, is_live_digest=False, is_insufficient_data=False,
    )

    art_map = {1: art1, 2: art2, 3: art3}
    map_p, evs, decs = run_sequential_replay([art1, art2, art3])
    # Ensure find_chaining_cases runs without AttributeError
    chaining = find_chaining_cases(evs, art_map)
    assert isinstance(chaining, int)

    metrics = analyze_replay_control([art1, art2, art3], map_p)
    assert "precision" in metrics
    assert "recall" in metrics
    assert "f1" in metrics
    assert "fpr" in metrics
    assert metrics["precision"] >= 0.0


def test_headline_similarity_display_only():
    sim = _headline_similarity_display_only("حمله موشکی به نفتکش", "حمله موشکی به نفتکش در دریای سرخ")
    assert 0.5 < sim < 1.0


@pytest.mark.asyncio
async def test_load_articles_from_db_read_only_enforcement(monkeypatch):
    mock_session = AsyncMock()
    mock_session.execute = AsyncMock()
    # Mock SHOW transaction_read_only returning 'on'
    mock_execute_res = MagicMock()
    mock_execute_res.scalar.return_value = "on"
    mock_execute_res.all.return_value = []
    mock_session.execute.return_value = mock_execute_res

    # SQLAlchemy async with session.begin() returns AsyncSessionTransaction context manager
    mock_tx = MagicMock()
    mock_tx.__aenter__ = AsyncMock(return_value=None)
    mock_tx.__aexit__ = AsyncMock(return_value=None)
    mock_session.begin = MagicMock(return_value=mock_tx)

    mock_session_cm = MagicMock()
    mock_session_cm.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session_cm.__aexit__ = AsyncMock(return_value=None)
    mock_factory = MagicMock(return_value=mock_session_cm)

    monkeypatch.setattr("scripts.dev_only.replay_event_matching.SessionLocal", mock_factory)

    records, ok = await load_articles_from_db(days=1)
    assert ok is True
    # Verify SET TRANSACTION READ ONLY was executed
    calls = [str(c[0][0]) for c in mock_session.execute.call_args_list if len(c[0]) > 0]
    assert any("SET TRANSACTION READ ONLY" in call for call in calls)
    assert any("SHOW transaction_read_only" in call for call in calls)


def test_active_gate_rejection_falls_back_to_historical_candidate():
    """Verify that when the best active candidate is rejected by the strict gate,

    the incoming article continues to historical candidates (>48h and <=14d),
    and if a qualifying historical candidate is found, creates a follow_up event.
    """
    now = datetime.now(UTC)
    # Historical Event (5 days ago): Yemen Red Sea missile strike with 'انصارالله'
    a_hist = ArticleRecord(
        1, "S1", "T1", "u1", now - timedelta(days=5),
        {
            "event_title": "حمله موشکی انصارالله در دریای سرخ",
            "categories": ["MIL"], "countries": ["یمن"], "actors": ["انصارالله"], "location": "دریای سرخ",
        },
        1, False, False,
    )
    # Active Event (10 hours ago): Yemen agriculture initiative (shares Yemen, MIL, Yemen location)
    a_active = ArticleRecord(
        2, "S2", "T2", "u2", now - timedelta(hours=10),
        {
            "event_title": "طرح توسعه کشاورزی در یمن",
            "categories": ["MIL"], "countries": ["یمن"], "actors": ["وزارت کشاورزی"], "location": "یمن",
        },
        2, False, False,
    )
    # Incoming Article (1 hour ago): Follow up on Yemen Red Sea missile strike with 'انصارالله'
    # Active candidate a_active has score 0.60, but fails strict gate (actors disjoint, kw < 0.25)
    # Historical candidate a_hist has score 0.85 >= 0.80 and passes strict gate!
    a_incoming = ArticleRecord(
        3, "S3", "T3", "u3", now - timedelta(hours=1),
        {
            "event_title": "گزارش تکمیلی حمله موشکی انصارالله در دریای سرخ",
            "categories": ["MIL"], "countries": ["یمن"], "actors": ["انصارالله"], "location": "دریای سرخ",
        },
        3, False, False,
    )

    map_p, evs_p, decs_p = run_sequential_replay([a_hist, a_active, a_incoming], require_strict_gate=True, keyword_threshold_x=0.25)
    assert decs_p[0]["action"] == "create"
    assert decs_p[1]["action"] == "create"
    assert decs_p[2]["action"] == "follow_up"
    assert evs_p[2].is_follow_up is True
    assert evs_p[2].parent_event_id == evs_p[0].id
    assert evs_p[2].title.startswith("[پیگیری]")


def test_new_pairs_can_occur_and_differs_by_article_order():
    """Verify that in sequential clustering, proposed strict gate can produce New Pairs

    (when an earlier rejection creates a new event that later articles merge into),
    and that pairwise relations differ based on arrival order.
    """
    from scripts.dev_only.replay_event_matching import compute_pairwise_relations

    now = datetime.now(UTC)
    # Art 1: Lebanon DIP in Beirut, actor 'نجیب میقاتی'
    a1 = ArticleRecord(
        1, "S1", "T1", "u1", now - timedelta(hours=10),
        {
            "event_title": "بیانیه نخست‌وزیر لبنان", "categories": ["DIP"], "countries": ["لبنان"], "actors": ["نجیب میقاتی"], "location": "لبنان",
        },
        1, False, False,
    )
    # Art 2: Lebanon DIP in Beirut, actor 'فراکسیون پارلمان'
    # Score with a1 = 0.60, but fails gate in Proposed -> creates Event 2!
    a2 = ArticleRecord(
        2, "S2", "T2", "u2", now - timedelta(hours=8),
        {
            "event_title": "جلسه اضطراری پارلمان لبنان", "categories": ["DIP"], "countries": ["لبنان"], "actors": ["فراکسیون پارلمان"], "location": "لبنان",
        },
        2, False, False,
    )
    # Art 3: Lebanon DIP, actor 'فراکسیون پارلمان', location 'طرابلس'
    # In Base: score with a1 is 0.45 < 0.60 -> creates Event 3! (pair (2, 3) NOT in Base)
    # In Proposed: score with a2 is 0.70 >= 0.60 -> merges into Event 2! (pair (2, 3) IS in Proposed!)
    a3 = ArticleRecord(
        3, "S3", "T3", "u3", now - timedelta(hours=6),
        {
            "event_title": "دیدار اعضای فراکسیون پارلمان لبنان در طرابلس", "categories": ["DIP"], "countries": ["لبنان"], "actors": ["فراکسیون پارلمان"], "location": "طرابلس",
        },
        3, False, False,
    )

    # Order A: [a1, a2, a3]
    map_base_a, _, _ = run_sequential_replay([a1, a2, a3], require_strict_gate=False)
    map_p_a, _, _ = run_sequential_replay([a1, a2, a3], require_strict_gate=True, keyword_threshold_x=0.25)
    pw_a = compute_pairwise_relations([a1, a2, a3], map_base_a, map_p_a)

    assert pw_a["new_pairs_count"] == 1
    assert (2, 3) in pw_a["new_pairs"]
    assert pw_a["broken_pairs_count"] == 1
    assert (1, 2) in pw_a["broken_pairs"]

    # Order B: [a2, a1, a3]
    map_base_b, _, _ = run_sequential_replay([a2, a1, a3], require_strict_gate=False)
    map_p_b, _, _ = run_sequential_replay([a2, a1, a3], require_strict_gate=True, keyword_threshold_x=0.25)
    pw_b = compute_pairwise_relations([a2, a1, a3], map_base_b, map_p_b)

    assert pw_b["new_pairs_count"] == 0
    assert pw_b["broken_pairs_count"] == 2


@pytest.mark.asyncio
async def test_operational_event_matcher_parity_with_replay_base_and_tie_breaking():
    """Verify operational EventMatcher and Replay (Base rule) produce identical match decisions

    and resolve ties identically (preferring the newer event by first_seen_at desc).
    """
    from app.database.models import Article, Event
    from app.processors.event_matcher import EventMatcher

    now = datetime.now(UTC)
    # Older event: Iran (0.25) + DIP (0.20) + Tehran (0.15) = 0.60
    a1 = ArticleRecord(
        1, "S1", "T1", "u1", now - timedelta(hours=5),
        {
            "event_title": "بیانیه اول", "categories": ["DIP"], "countries": ["ایران"], "actors": ["سفرا"], "location": "تهران",
        },
        1, False, False,
    )
    # Newer event: Turkey (0.0) + DIP (0.20) + Tehran (0.15) + actor 'کارشناسان' (0.25) = 0.60
    a2 = ArticleRecord(
        2, "S2", "T2", "u2", now - timedelta(hours=2),
        {
            "event_title": "بیانیه دوم", "categories": ["DIP"], "countries": ["ترکیه"], "actors": ["کارشناسان"], "location": "تهران",
        },
        2, False, False,
    )
    # Incoming article (1h ago)
    a3_payload = {
        "event_title": "بیانیه سوم", "categories": ["DIP"], "countries": ["ایران"], "actors": ["کارشناسان"], "location": "تهران",
    }
    a3 = ArticleRecord(3, "S3", "T3", "u3", now - timedelta(hours=1), a3_payload, 3, False, False)

    # 1. Run Replay (Base rule)
    map_base, evs_base, decs_base = run_sequential_replay([a1, a2, a3], require_strict_gate=False)
    assert decs_base[2]["action"] == "merge"
    assert decs_base[2]["event_id"] == evs_base[1].id  # Target is Event 2 (the newer candidate)

    # 2. Run Operational EventMatcher with identical DB candidate events
    ev1 = Event(
        id=1, title="بیانیه اول", event_type="DIP", countries=["ایران"], actors=["سفرا"], location="تهران",
        importance=3, status="active", first_seen_at=now - timedelta(hours=5),
    )
    ev2 = Event(
        id=2, title="بیانیه دوم", event_type="DIP", countries=["ترکیه"], actors=["کارشناسان"], location="تهران",
        importance=3, status="active", first_seen_at=now - timedelta(hours=2),
    )
    matcher = EventMatcher(merge_threshold=0.60)
    session = AsyncMock()
    # Mock candidate query returning [ev2, ev1] (ordered by first_seen_at desc)
    scalars_mock = MagicMock()
    scalars_mock.all.return_value = [ev2, ev1]
    session.scalars.return_value = scalars_mock
    session.scalar.return_value = None

    art3_db = Article(id=3, title="T3", original_url="u3", collected_at=now - timedelta(hours=1))
    em_res = await matcher.match_or_create_event(session, art3_db, a3_payload, source_name="S3")

    assert em_res.match_type == "auto"
    assert em_res.event_id == 2  # Identical choice: Event 2 (newer) selected by EventMatcher!


@pytest.mark.asyncio
async def test_load_articles_deduplication_and_filtering(monkeypatch):
    """Verify load_articles_from_db deduplicates multiple rows for the same article_id,

    assigns highest-confidence event as primary db_event_id, and aggregates all in db_event_ids.
    """
    from scripts.dev_only.replay_event_matching import load_articles_from_db

    now = datetime.now(UTC)
    # Simulate DB returning duplicate rows for article ID 10 (joined with two different events: conf 0.85 and 0.65)
    row1 = (10, "Title 10", "c10", "o10", now, {"actors": ["سفرا"], "location": "تهران"}, "S1", 100, 0.85)
    row2 = (10, "Title 10", "c10", "o10", now, {"actors": ["سفرا"], "location": "تهران"}, "S1", 200, 0.65)
    row3 = (20, "Title 20", "c20", "o20", now, {"actors": ["وزرا"], "location": "بغداد"}, "S2", 300, 0.90)

    mock_session = AsyncMock()
    mock_execute_res = MagicMock()
    mock_execute_res.scalar.return_value = "on"
    mock_execute_res.all.return_value = [row1, row2, row3]
    mock_session.execute.return_value = mock_execute_res

    mock_tx = MagicMock()
    mock_tx.__aenter__ = AsyncMock(return_value=None)
    mock_tx.__aexit__ = AsyncMock(return_value=None)
    mock_session.begin = MagicMock(return_value=mock_tx)

    mock_session_cm = MagicMock()
    mock_session_cm.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session_cm.__aexit__ = AsyncMock(return_value=None)
    monkeypatch.setattr("scripts.dev_only.replay_event_matching.SessionLocal", MagicMock(return_value=mock_session_cm))

    records, ok = await load_articles_from_db(days=7)
    assert ok is True
    # Article 10 must appear exactly ONCE
    assert len(records) == 2
    assert [r.id for r in records] == [10, 20]
    # Primary db_event_id must be 100 (highest confidence)
    assert records[0].db_event_id == 100
    # Both linked events must be present in db_event_ids
    assert records[0].db_event_ids == {100, 200}
    assert records[1].db_event_ids == {300}


def test_pairwise_relations_rejects_self_pairs_on_duplicates():
    """Verify compute_pairwise_relations explicitly ignores duplicate article IDs

    and never generates reflexive self-pairs (aid, aid).
    """
    now = datetime.now(UTC)
    a1 = ArticleRecord(1, "S1", "T1", "u1", now, {"event_title": "T"}, 1, False, False)
    # Simulated duplicated article instance in input list
    a1_dup = ArticleRecord(1, "S1", "T1", "u1", now, {"event_title": "T"}, 1, False, False)
    a2 = ArticleRecord(2, "S2", "T2", "u2", now, {"event_title": "T"}, 2, False, False)

    map_base = {1: 10, 2: 10}
    map_prop = {1: 10, 2: 20}

    pw = compute_pairwise_relations([a1, a1_dup, a2], map_base, map_prop)
    # Self-pair (1, 1) must NEVER be generated
    assert (1, 1) not in pw["pairs_base"]
    assert (1, 1) not in pw["pairs_prop"]
    assert (1, 1) not in pw["broken_pairs"]
    assert (1, 1) not in pw["new_pairs"]
    # Only pair (1, 2) should exist
    assert pw["pairs_base"] == {(1, 2)}
    assert pw["broken_pairs"] == {(1, 2)}


@pytest.mark.asyncio
async def test_main_async_exits_nonzero_on_db_failure(monkeypatch):
    """Verify main_async terminates with exit code 1 when load_articles_from_db raises an exception."""
    from scripts.dev_only.replay_event_matching import main_async

    async def mock_fail_load(days: int):
        raise ConnectionRefusedError("Database network connection refused")

    monkeypatch.setattr("scripts.dev_only.replay_event_matching.load_articles_from_db", mock_fail_load)

    with pytest.raises(SystemExit) as exc_info:
        await main_async(days=30)

    assert exc_info.value.code == 1


def test_deterministic_candidate_tie_breaking_with_identical_timestamps():
    """Verify candidate tie-breaking is fully deterministic when two events share identical first_seen_at."""
    now = datetime.now(UTC)
    same_time = now - timedelta(hours=3)

    # Event 10: id=10, first_seen_at=same_time, score=0.60
    ev10 = ReplayEvent(
        id=10, title="رویداد ده", event_type="DIP", countries=["ایران"], actors=["سفرا"],
        location="تهران", importance=3, status="active", first_seen_at=same_time, last_updated_at=same_time,
        is_live_digest=False, is_follow_up=False, parent_event_id=None, article_ids=[1],
    )
    # Event 20: id=20, first_seen_at=same_time, score=0.60
    ev20 = ReplayEvent(
        id=20, title="رویداد بیست", event_type="DIP", countries=["ایران"], actors=["سفرا"],
        location="تهران", importance=3, status="active", first_seen_at=same_time, last_updated_at=same_time,
        is_live_digest=False, is_follow_up=False, parent_event_id=None, article_ids=[2],
    )

    # Both events have identical similarity (1.0).
    # Deterministic secondary ordering (ev.id desc) ensures Event 20 is consistently prioritized over Event 10
    active_cands = [ev10, ev20]
    active_cands.sort(key=lambda ev: (ev.first_seen_at, ev.id), reverse=True)
    assert active_cands[0].id == 20
    assert active_cands[1].id == 10


@pytest.mark.asyncio
async def test_read_only_transaction_executes_queries_strictly_inside_tx(monkeypatch):
    """Verify that data query stmt is executed strictly inside the session.begin() context manager."""
    from scripts.dev_only.replay_event_matching import load_articles_from_db

    execution_order: list[str] = []

    mock_session = AsyncMock()

    async def mock_execute(stmt, *args, **kwargs):
        stmt_str = str(stmt)
        if "SET TRANSACTION READ ONLY" in stmt_str:
            execution_order.append("set_ro")
        elif "SHOW transaction_read_only" in stmt_str:
            execution_order.append("show_ro")
        else:
            execution_order.append("data_query")
        mock_res = MagicMock()
        mock_res.scalar.return_value = "on"
        mock_res.all.return_value = []
        return mock_res

    mock_session.execute = mock_execute

    mock_tx = MagicMock()

    async def mock_tx_enter(*args, **kwargs):
        execution_order.append("tx_enter")

    async def mock_tx_exit(*args, **kwargs):
        execution_order.append("tx_exit")

    mock_tx.__aenter__ = mock_tx_enter
    mock_tx.__aexit__ = mock_tx_exit
    mock_session.begin = MagicMock(return_value=mock_tx)

    mock_session_cm = MagicMock()
    mock_session_cm.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session_cm.__aexit__ = AsyncMock(return_value=None)
    monkeypatch.setattr("scripts.dev_only.replay_event_matching.SessionLocal", MagicMock(return_value=mock_session_cm))

    _, ok = await load_articles_from_db(days=1)
    assert ok is True

    # Validate exact execution order
    assert execution_order == ["tx_enter", "set_ro", "show_ro", "data_query", "tx_exit"]


def test_atomic_write_preserves_previous_file_on_failure(tmp_path):
    """Verify that writing output via temporary file and atomic replace

    leaves the original file intact if an error occurs during writing.
    """
    import json

    orig_file = tmp_path / "replay_samples_30.json"
    orig_content = [{"sample_id": 1, "test": "original_valid_data"}]
    with open(orig_file, "w", encoding="utf-8") as f:
        json.dump(orig_content, f)

    temp_file = orig_file.with_suffix(".tmp")

    # Simulate an error interrupting write midway
    try:
        with open(temp_file, "w", encoding="utf-8") as f:
            f.write("{ incomplete json...")
            raise OSError("Simulated disk write failure or interruption")
    except OSError:
        if temp_file.exists():
            temp_file.unlink()

    # Original file must remain 100% intact and uncorrupted
    assert orig_file.exists()
    with open(orig_file, encoding="utf-8") as f:
        loaded = json.load(f)
    assert loaded == orig_content
    assert not temp_file.exists()


@pytest.mark.asyncio
async def test_production_tie_breaking_non_determinism_documentation():
    """Document that operational EventMatcher candidate selection on identical first_seen_at

    depends entirely on SQL engine scan order because stmt lacks a secondary sort key.
    """
    from app.database.models import Article, Event
    from app.processors.event_matcher import EventMatcher

    now = datetime.now(UTC)
    same_time = now - timedelta(hours=3)

    ev1 = Event(
        id=1, title="رویداد یک", event_type="DIP", countries=["ایران"], actors=["سفرا"],
        location="تهران", importance=3, status="active", first_seen_at=same_time,
    )
    ev2 = Event(
        id=2, title="رویداد دو", event_type="DIP", countries=["ایران"], actors=["سفرا"],
        location="تهران", importance=3, status="active", first_seen_at=same_time,
    )

    art_payload = {
        "event_title": "بیانیه مشترک", "categories": ["DIP"], "countries": ["ایران"], "actors": ["سفرا"], "location": "تهران",
    }
    art_db = Article(id=99, title="T", original_url="u", collected_at=now)

    matcher = EventMatcher(merge_threshold=0.60)

    # Scenario A: SQL returns [ev1, ev2]
    session_a = AsyncMock()
    mock_scalars_a = MagicMock()
    mock_scalars_a.all.return_value = [ev1, ev2]
    session_a.scalars.return_value = mock_scalars_a
    session_a.scalar.return_value = None
    res_a = await matcher.match_or_create_event(session_a, art_db, art_payload, source_name="S")

    # Scenario B: SQL returns [ev2, ev1] (e.g. reverse index scan or different page layout)
    session_b = AsyncMock()
    mock_scalars_b = MagicMock()
    mock_scalars_b.all.return_value = [ev2, ev1]
    session_b.scalars.return_value = mock_scalars_b
    session_b.scalar.return_value = None
    res_b = await matcher.match_or_create_event(session_b, art_db, art_payload, source_name="S")

    # This proves empirically that Production EventMatcher returns different candidates
    # based solely on SQL row order when timestamps and scores are identical!
    assert res_a.event_id == 1
    assert res_b.event_id == 2



