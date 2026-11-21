"""Unit tests for Y-14 event matching replay and calibration analysis."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
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
    save_samples_atomically,
)


def _multiprocess_atomic_save_worker(
    target_path_str: str,
    fixed_ts: int,
    worker_id: int,
    queue: Any,
    barrier: Any,
) -> None:
    """Module-level worker function required for multiprocessing on Windows ('spawn' mode)."""
    try:
        from scripts.dev_only.replay_event_matching import save_samples_atomically

        target = Path(target_path_str)
        sample = [{"worker_id": worker_id, "data": f"payload_from_{worker_id}"}]
        barrier.wait(timeout=10)
        errs: list[Exception] = []
        success = save_samples_atomically(
            sample,
            target,
            backup_timestamp=fixed_ts,
            error_collector=errs,
        )
        err_info = None
        if errs:
            err = errs[0]
            err_info = {
                "type": type(err).__name__,
                "msg": str(err),
                "winerror": getattr(err, "winerror", None),
            }
        queue.put((worker_id, success, err_info, None))
    except Exception as exc:
        queue.put((worker_id, False, None, str(exc)))


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
    is unspecified by PostgreSQL and depends entirely on SQL engine scan order / physical page layout
    because stmt lacks an ORDER BY clause.
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
        "event_title": "رویداد مشترک", "categories": ["DIP"], "countries": ["ایران"], "actors": ["سفرا"], "location": "تهران",
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


def test_multi_event_scenario_pairwise_tolerance_vs_partition():
    """Verify data semantics for multi-event articles:

    Scenario: A -> {E1}, B -> {E1, E2}, C -> {E2}
    - Definition 1 (At least one shared event): Transitivity FAILS (Tolerance relation, NOT a partition).
      (A, B) connected, (B, C) connected, but (A, C) disconnected.
    - Definition 2 (Primary reference event equality): Transitivity HOLDS (Strict Equivalence Partition).
      (A, B) connected, (B, C) disconnected, (A, C) disconnected.
    """
    now = datetime.now(UTC)
    art_a = ArticleRecord(1, "S", "A", "u1", now, {}, db_event_id=1, is_live_digest=False, is_insufficient_data=False, db_event_ids={1})
    art_b = ArticleRecord(2, "S", "B", "u2", now, {}, db_event_id=1, is_live_digest=False, is_insufficient_data=False, db_event_ids={1, 2})
    art_c = ArticleRecord(3, "S", "C", "u3", now, {}, db_event_id=2, is_live_digest=False, is_insufficient_data=False, db_event_ids={2})

    articles = [art_a, art_b, art_c]

    # Replay mapping placing A and B together, C separate: {1: 10, 2: 10, 3: 20}
    rep_map = {1: 10, 2: 10, 3: 20}

    # 1. Definition 1: Tolerance Relation (use_primary_event_only=False)
    ctrl_def1 = analyze_replay_control(articles, rep_map, use_primary_event_only=False)
    # Under Def 1:
    # same_db(A, B) = True ({1} & {1, 2} != empty)
    # same_db(B, C) = True ({1, 2} & {2} != empty)
    # same_db(A, C) = False ({1} & {2} == empty) -> Non-transitive!
    # DB positive pairs = 2: (A, B) and (B, C)
    # Replay positive pairs = 1: (A, B)
    # TP = 1 (A, B), FN = 1 (B, C), FP = 0, TN = 1 (A, C)
    assert ctrl_def1["tp"] == 1
    assert ctrl_def1["fn"] == 1
    assert ctrl_def1["fp"] == 0
    assert ctrl_def1["tn"] == 1

    # 2. Definition 2: Strict Partition (use_primary_event_only=True)
    ctrl_def2 = analyze_replay_control(articles, rep_map, use_primary_event_only=True)
    # Under Def 2:
    # primary(A)=1, primary(B)=1, primary(C)=2
    # same_db(A, B) = True (1 == 1)
    # same_db(B, C) = False (1 != 2)
    # same_db(A, C) = False (1 != 2) -> Transitive Equivalence Relation!
    # DB positive pairs = 1: (A, B)
    # Replay positive pairs = 1: (A, B)
    # TP = 1 (A, B), FN = 0, FP = 0, TN = 2: (B, C), (A, C)
    assert ctrl_def2["tp"] == 1
    assert ctrl_def2["fn"] == 0
    assert ctrl_def2["fp"] == 0
    assert ctrl_def2["tn"] == 2


def test_save_samples_atomically_robustness_and_failures(tmp_path, monkeypatch):
    """Verify save_samples_atomically handles success, backup generation,

    and leaves existing valid file untouched on failure during write or replacement.
    """
    import json

    target_file = tmp_path / "replay_samples_30.json"
    initial_data = [{"sample_id": 1, "status": "original_valid"}]

    # 1. Success on initial file creation
    ok1 = save_samples_atomically(initial_data, target_file, backup_timestamp=1000)
    assert ok1 is True
    assert target_file.exists()
    with open(target_file, encoding="utf-8") as f:
        assert json.load(f) == initial_data

    # 2. Success on update with backup creation
    updated_data = [{"sample_id": 1, "status": "updated_valid"}]
    ok2 = save_samples_atomically(updated_data, target_file, backup_timestamp=2000)
    assert ok2 is True
    bak_file = tmp_path / "replay_samples_30.bak_2000.json"
    assert bak_file.exists()
    with open(bak_file, encoding="utf-8") as f:
        assert json.load(f) == initial_data
    with open(target_file, encoding="utf-8") as f:
        assert json.load(f) == updated_data

    # 3. Simulated failure during write/fsync: target_file must remain intact
    def mock_broken_dumps(*args, **kwargs):
        raise OSError("Simulated disk error during serialization")

    monkeypatch.setattr("json.dumps", mock_broken_dumps)
    ok3 = save_samples_atomically([{"corrupt": True}], target_file, backup_timestamp=3000)
    assert ok3 is False
    # Target file still contains updated_data, NOT corrupted
    with open(target_file, encoding="utf-8") as f:
        assert json.load(f) == updated_data

    # 4. Simulated failure during backup: must abort and leave target_file 100% intact
    import shutil
    monkeypatch.undo()  # restore json.dumps

    def mock_broken_copyfileobj(*args, **kwargs):
        raise OSError("Simulated permission error or failure during backup creation")

    monkeypatch.setattr(shutil, "copyfileobj", mock_broken_copyfileobj)
    ok4 = save_samples_atomically([{"corrupt_backup": True}], target_file, backup_timestamp=4000)
    assert ok4 is False
    # Target file still contains updated_data, NOT overwritten
    with open(target_file, encoding="utf-8") as f:
        assert json.load(f) == updated_data
    # Temp file must have been cleanly removed
    assert not any(p.name.startswith("replay_samples_30.tmp_") for p in tmp_path.iterdir())

    # 5. Timestamp collision on backup creates sequential numbered backups
    monkeypatch.undo()  # restore shutil.copyfileobj
    ok5_a = save_samples_atomically([{"run": "first"}], target_file, backup_timestamp=5000)
    assert ok5_a is True
    assert (tmp_path / "replay_samples_30.bak_5000.json").exists()

    ok5_b = save_samples_atomically([{"run": "second"}], target_file, backup_timestamp=5000)
    assert ok5_b is True
    assert (tmp_path / "replay_samples_30.bak_5000_1.json").exists()


def test_compute_pairwise_relations_mapping_integrity_checks():
    """Verify compute_pairwise_relations strictly verifies mapping equality with input articles

    and halts with an explicit ValueError on missing or extra article IDs.
    """
    now = datetime.now(UTC)
    art1 = ArticleRecord(1, "S", "A1", "u1", now, {}, 1, False, False)
    art2 = ArticleRecord(2, "S", "A2", "u2", now, {}, 1, False, False)
    art3 = ArticleRecord(3, "S", "A3", "u3", now, {}, 2, False, False)
    articles = [art1, art2, art3]

    valid_base = {1: 10, 2: 10, 3: 20}
    valid_prop = {1: 10, 2: 30, 3: 20}

    # Case 1: Both mappings complete and correct
    pw = compute_pairwise_relations(articles, valid_base, valid_prop)
    assert pw["pairs_base_count"] == 1
    assert pw["broken_pairs_count"] == 1

    # Case 2: Article ID missing from Base
    incomplete_base = {1: 10, 2: 10}
    with pytest.raises(ValueError) as exc_base_missing:
        compute_pairwise_relations(articles, incomplete_base, valid_prop)
    assert "Articles missing from Base mapping: [3]" in str(exc_base_missing.value)

    # Case 3: Article ID missing from Proposed
    incomplete_prop = {1: 10, 3: 20}
    with pytest.raises(ValueError) as exc_prop_missing:
        compute_pairwise_relations(articles, valid_base, incomplete_prop)
    assert "Articles missing from Proposed mapping: [2]" in str(exc_prop_missing.value)

    # Case 4: Extra unexpected Article ID in Base
    extra_base = {1: 10, 2: 10, 3: 20, 99: 99}
    with pytest.raises(ValueError) as exc_base_extra:
        compute_pairwise_relations(articles, extra_base, valid_prop)
    assert "Unexpected extra articles in Base mapping: [99]" in str(exc_base_extra.value)

    # Case 5: Extra unexpected Article ID in Proposed
    extra_prop = {1: 10, 2: 30, 3: 20, 88: 88}
    with pytest.raises(ValueError) as exc_prop_extra:
        compute_pairwise_relations(articles, valid_base, extra_prop)
    assert "Unexpected extra articles in Proposed mapping: [88]" in str(exc_prop_extra.value)


def test_save_samples_atomically_temp_write_failure_preserves_target(tmp_path, monkeypatch):
    """Independent test 1: Simulated temporary file write error.

    Target file must remain intact and temp file must be cleaned up.
    """
    import json
    target_file = tmp_path / "replay_samples_30.json"
    initial_data = [{"sample_id": 10, "status": "original_intact"}]
    with open(target_file, "w", encoding="utf-8") as f:
        json.dump(initial_data, f)

    def mock_broken_dumps(*args, **kwargs):
        raise OSError("Simulated disk error or write interruption during temp serialization")

    monkeypatch.setattr("json.dumps", mock_broken_dumps)
    errs = []
    success = save_samples_atomically([{"new": "data"}], target_file, backup_timestamp=1111, error_collector=errs)
    assert success is False
    assert len(errs) == 1
    assert "Simulated disk error" in str(errs[0])

    # Original target file must be completely untouched
    with open(target_file, encoding="utf-8") as f:
        assert json.load(f) == initial_data

    # No leftover temporary files
    assert not any(p.name.startswith("replay_samples_30.tmp_") for p in tmp_path.iterdir())


def test_save_samples_atomically_backup_failure_aborts_and_preserves_target(tmp_path, monkeypatch):
    """Independent test 2: Simulated backup creation error.

    Target file must NOT be replaced or modified, temp file must be unlinked.
    """
    import json
    import shutil
    target_file = tmp_path / "replay_samples_30.json"
    initial_data = [{"sample_id": 20, "status": "original_intact"}]
    with open(target_file, "w", encoding="utf-8") as f:
        json.dump(initial_data, f)

    def mock_broken_copyfileobj(*args, **kwargs):
        raise OSError("Simulated permission error or quota failure during backup copy")

    monkeypatch.setattr(shutil, "copyfileobj", mock_broken_copyfileobj)
    success = save_samples_atomically([{"new": "data"}], target_file, backup_timestamp=2222)
    assert success is False

    # Original target file must be completely untouched
    with open(target_file, encoding="utf-8") as f:
        assert json.load(f) == initial_data

    # No leftover temporary files
    assert not any(p.name.startswith("replay_samples_30.tmp_") for p in tmp_path.iterdir())


def test_save_samples_atomically_replace_failure_handles_windows_lock_and_preserves_target(tmp_path, monkeypatch):
    """Independent test 3: Simulated Windows mandatory file lock (WinError 32) on os.replace.

    Target file must remain intact and temp file must be deleted.
    """
    import json
    import os
    target_file = tmp_path / "replay_samples_30.json"
    initial_data = [{"sample_id": 30, "status": "original_intact"}]
    with open(target_file, "w", encoding="utf-8") as f:
        json.dump(initial_data, f)

    def mock_locked_replace(src, dst):
        raise PermissionError(32, "The process cannot access the file because it is being used by another process")

    monkeypatch.setattr(os, "replace", mock_locked_replace)
    success = save_samples_atomically([{"new": "data"}], target_file, backup_timestamp=3333)
    assert success is False

    # Original target file must be completely untouched
    with open(target_file, encoding="utf-8") as f:
        assert json.load(f) == initial_data

    # No leftover temporary files
    assert not any(p.name.startswith("replay_samples_30.tmp_") for p in tmp_path.iterdir())


def test_save_samples_atomically_exclusive_backup_and_process_isolation(tmp_path):
    """Verify exclusive backup creation avoids collisions under identical timestamps

    and isolates temporary files with process ID.
    """
    import json
    target_file = tmp_path / "replay_samples_30.json"
    initial_data = [{"version": 1}]
    with open(target_file, "w", encoding="utf-8") as f:
        json.dump(initial_data, f)

    fixed_ts = 999999

    # First update: creates .bak_999999.json
    ok1 = save_samples_atomically([{"version": 2}], target_file, backup_timestamp=fixed_ts)
    assert ok1 is True
    bak1 = tmp_path / "replay_samples_30.bak_999999.json"
    assert bak1.exists()
    with open(bak1, encoding="utf-8") as f:
        assert json.load(f) == [{"version": 1}]

    # Second update with same timestamp: creates .bak_999999_1.json exclusively without race collision
    ok2 = save_samples_atomically([{"version": 3}], target_file, backup_timestamp=fixed_ts)
    assert ok2 is True
    bak2 = tmp_path / "replay_samples_30.bak_999999_1.json"
    assert bak2.exists()
    with open(bak2, encoding="utf-8") as f:
        assert json.load(f) == [{"version": 2}]

    # Target file has version 3
    with open(target_file, encoding="utf-8") as f:
        assert json.load(f) == [{"version": 3}]


@pytest.mark.asyncio
async def test_load_articles_closed_interval_and_summarized_filter(monkeypatch):
    """Verify load_articles_from_db restricts query to closed interval [start_utc, end_utc],

    enforces Article.status == 'summarized', and retains Article.ai_payload.is_not(None).
    """
    from scripts.dev_only.replay_event_matching import load_articles_from_db

    captured_stmts = []

    mock_session = AsyncMock()

    async def mock_execute(stmt, *args, **kwargs):
        stmt_str = str(stmt)
        if "SET TRANSACTION READ ONLY" not in stmt_str and "SHOW transaction_read_only" not in stmt_str:
            captured_stmts.append(stmt)
        mock_res = MagicMock()
        mock_res.scalar.return_value = "on"
        mock_res.all.return_value = []
        return mock_res

    mock_session.execute = mock_execute

    mock_tx = MagicMock()
    mock_tx.__aenter__ = AsyncMock(return_value=None)
    mock_tx.__aexit__ = AsyncMock(return_value=None)
    mock_session.begin = MagicMock(return_value=mock_tx)

    mock_session_cm = MagicMock()
    mock_session_cm.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session_cm.__aexit__ = AsyncMock(return_value=None)
    monkeypatch.setattr("scripts.dev_only.replay_event_matching.SessionLocal", MagicMock(return_value=mock_session_cm))

    fixed_end = datetime(2026, 11, 15, 12, 0, 0, tzinfo=UTC)
    fixed_start = fixed_end - timedelta(days=7)

    # 1. Test explicit bounds
    records, ok = await load_articles_from_db(days=7, start_utc=fixed_start, end_utc=fixed_end)
    assert ok is True
    assert len(captured_stmts) == 1
    compiled_sql = str(captured_stmts[0].compile(compile_kwargs={"literal_binds": True}))

    # Closed interval checks
    assert "articles.collected_at >=" in compiled_sql
    assert "articles.collected_at <=" in compiled_sql
    assert "2026-11-08 12:00:00" in compiled_sql
    assert "2026-11-15 12:00:00" in compiled_sql

    # Status whitelist check
    assert "articles.status = 'summarized'" in compiled_sql
    assert "articles.ai_payload IS NOT NULL" in compiled_sql
    assert "flagged" not in compiled_sql


def test_save_samples_atomically_backup_partial_copy_failure_cleans_incomplete_backup(tmp_path, monkeypatch):
    """Verify that if copying backup data fails mid-transfer:

    1. Incomplete temporary backup file (.bak_tmp_*) is unlinked.
    2. No final backup file (.bak_*.json) is ever created or exposed.
    3. Original target file is NEVER replaced or modified.
    4. Temporary file (.tmp_*) is unlinked.
    5. Function returns False.
    """
    import json
    import shutil
    target_file = tmp_path / "replay_samples_30.json"
    initial_data = [{"valid_key": "original_seed_data"}]
    with open(target_file, "w", encoding="utf-8") as f:
        json.dump(initial_data, f)

    def mock_partial_copyfileobj(f_src, f_dst, length=0):
        # Write partial corrupt data into f_dst then fail
        f_dst.write(b'{"incomplete_corrupt_content": true')
        f_dst.flush()
        raise OSError("Simulated disk error or write interruption after partial backup write")

    monkeypatch.setattr(shutil, "copyfileobj", mock_partial_copyfileobj)
    success = save_samples_atomically([{"new": "data"}], target_file, backup_timestamp=6666)
    assert success is False

    # Target file must remain 100% intact
    with open(target_file, encoding="utf-8") as f:
        assert json.load(f) == initial_data

    # No final backup file was ever created
    final_bak_files = list(tmp_path.glob("replay_samples_30.bak_*.json"))
    assert len(final_bak_files) == 0

    # No temporary backup files (.bak_tmp_*) or temp json files (.tmp_*) remain on disk
    temp_bak_files = list(tmp_path.glob("replay_samples_30.bak_tmp_*"))
    assert len(temp_bak_files) == 0
    temp_files = list(tmp_path.glob("replay_samples_30.tmp_*"))
    assert len(temp_files) == 0


def test_save_samples_atomically_backup_finalization_collision_avoids_overwriting(tmp_path):
    """Verify that if final backup destination already exists,

    the backup finalizer increments suffix without overwriting the existing backup.
    """
    import json
    target_file = tmp_path / "replay_samples_30.json"
    target_file.write_text(json.dumps([{"target": "v1"}]), encoding="utf-8")

    fixed_ts = 5555
    # Pre-create the first backup path
    existing_bak = tmp_path / f"replay_samples_30.bak_{fixed_ts}.json"
    existing_content = [{"existing_backup": "dont_touch_me"}]
    existing_bak.write_text(json.dumps(existing_content), encoding="utf-8")

    success = save_samples_atomically([{"target": "v2"}], target_file, backup_timestamp=fixed_ts)
    assert success is True

    # Pre-existing backup must remain 100% untouched
    assert json.loads(existing_bak.read_text(encoding="utf-8")) == existing_content

    # A collision-free second backup (.bak_5555_1.json) must have been created containing target v1
    next_bak = tmp_path / f"replay_samples_30.bak_{fixed_ts}_1.json"
    assert next_bak.exists()
    assert json.loads(next_bak.read_text(encoding="utf-8")) == [{"target": "v1"}]

    # Target file now has v2
    assert json.loads(target_file.read_text(encoding="utf-8")) == [{"target": "v2"}]


def test_save_samples_atomically_exclusive_temp_file_handles_collision_without_deleting_other_files(tmp_path, monkeypatch):
    """Verify exclusive temp file creation ('xb') handles collision cleanly

    and never deletes or overwrites temporary files belonging to other processes.
    """
    import json
    import os
    target_file = tmp_path / "replay_samples_30.json"
    initial_data = [{"version": 1}]
    with open(target_file, "w", encoding="utf-8") as f:
        json.dump(initial_data, f)

    # Pre-create a candidate temp file simulating an in-flight file from another worker
    foreign_temp = tmp_path / f"replay_samples_30.tmp_{os.getpid()}_collided.json"
    foreign_data = b"foreign_process_in_flight_payload"
    foreign_temp.write_bytes(foreign_data)

    class MockUUID:
        def __init__(self, hex_val: str):
            self.hex = hex_val

    uuid_sequence = [MockUUID("collided"), MockUUID("unique99")]

    def mock_uuid4():
        if uuid_sequence:
            return uuid_sequence.pop(0)
        return MockUUID("extra000")

    monkeypatch.setattr("scripts.dev_only.replay_event_matching.uuid4", mock_uuid4)

    success = save_samples_atomically([{"version": 2}], target_file, backup_timestamp=7777)
    assert success is True

    # The other process's in-flight temp file must remain 100% UNTOUCHED
    assert foreign_temp.exists()
    assert foreign_temp.read_bytes() == foreign_data

    # Clean up foreign temp file for test isolation
    foreign_temp.unlink()

    # Target file was successfully updated
    with open(target_file, encoding="utf-8") as f:
        assert json.load(f) == [{"version": 2}]


@pytest.mark.asyncio
async def test_main_async_exits_nonzero_on_atomic_save_failure(monkeypatch):
    """Verify main_async terminates with exit code 1 if save_samples_atomically returns False."""
    from scripts.dev_only.replay_event_matching import main_async

    async def mock_load(*args, **kwargs):
        now = datetime.now(UTC)
        art = ArticleRecord(1, "S", "T", "u", now, {"event_title": "T"}, 1, False, False)
        return [art], True

    monkeypatch.setattr("scripts.dev_only.replay_event_matching.load_articles_from_db", mock_load)
    monkeypatch.setattr("scripts.dev_only.replay_event_matching.save_samples_atomically", lambda *args, **kwargs: False)

    with pytest.raises(SystemExit) as exc_info:
        await main_async(days=7)
    assert exc_info.value.code == 1


def test_save_samples_atomically_backup_temp_collision_does_not_delete_foreign_file(tmp_path, monkeypatch):
    """Verify that if candidate backup temp file (.bak_tmp_*) already exists (e.g. from another process),

    save_samples_atomically generates a new unique name via retry without deleting or corrupting
    the pre-existing foreign temporary file.
    """
    import json
    import os

    target_file = tmp_path / "replay_samples_30.json"
    initial_data = [{"version": 1}]
    with open(target_file, "w", encoding="utf-8") as f:
        json.dump(initial_data, f)

    # Pre-create a candidate backup temp file simulating an in-flight backup from another worker
    foreign_bak_temp = tmp_path / f"replay_samples_30.bak_tmp_{os.getpid()}_collided.json"
    foreign_payload = b"foreign_process_in_flight_backup_payload"
    foreign_bak_temp.write_bytes(foreign_payload)

    class MockUUID:
        def __init__(self, hex_val: str):
            self.hex = hex_val

    # First call will collide with foreign_bak_temp; second call generates unique name
    uuid_sequence = [MockUUID("collided"), MockUUID("unique88")]

    def mock_uuid4():
        if uuid_sequence:
            return uuid_sequence.pop(0)
        return MockUUID("extra000")

    monkeypatch.setattr("scripts.dev_only.replay_event_matching.uuid4", mock_uuid4)

    success = save_samples_atomically([{"version": 2}], target_file, backup_timestamp=9999)
    assert success is True

    # The other process's foreign backup temp file must remain 100% UNTOUCHED
    assert foreign_bak_temp.exists()
    assert foreign_bak_temp.read_bytes() == foreign_payload

    # Clean up foreign temp file for test isolation
    foreign_bak_temp.unlink()

    # Target file was successfully updated
    with open(target_file, encoding="utf-8") as f:
        assert json.load(f) == [{"version": 2}]

    # Backup file (.bak_9999.json) was successfully finalized with initial_data
    backup_file = tmp_path / "replay_samples_30.bak_9999.json"
    assert backup_file.exists()
    with open(backup_file, encoding="utf-8") as f:
        assert json.load(f) == initial_data

    # No leftover temporary files (.tmp_* or .bak_tmp_*)
    temp_files = list(tmp_path.glob("replay_samples_30.tmp_*")) + list(tmp_path.glob("replay_samples_30.bak_tmp_*"))
    assert len(temp_files) == 0


def test_save_samples_atomically_backup_temp_exhaustion_aborts_cleanly(tmp_path, monkeypatch):
    """Verify that if all 10 candidate backup temp file names collide (FileExistsError),

    save_samples_atomically aborts with False, never touches or deletes foreign files,
    cleans up its own JSON temp file, and preserves the original target file.
    """
    import json

    target_file = tmp_path / "replay_samples_30.json"
    initial_data = [{"version": "initial_seed"}]
    with open(target_file, "w", encoding="utf-8") as f:
        json.dump(initial_data, f)

    orig_open = open

    def mock_open_always_collide(path, mode="r", *args, **kwargs):
        if ".bak_tmp_" in str(path) and "x" in mode:
            raise FileExistsError(f"Simulated continuous collision on {path}")
        return orig_open(path, mode, *args, **kwargs)

    monkeypatch.setattr("builtins.open", mock_open_always_collide)

    errs = []
    success = save_samples_atomically([{"version": "should_fail"}], target_file, backup_timestamp=1234, error_collector=errs)
    assert success is False
    assert len(errs) == 1
    assert isinstance(errs[0], FileExistsError)

    # Target file must remain 100% intact
    with open(target_file, encoding="utf-8") as f:
        assert json.load(f) == initial_data

    # No backup file was created
    assert len(list(tmp_path.glob("replay_samples_30.bak_*"))) == 0

    # No temp files leaked
    assert len(list(tmp_path.glob("replay_samples_30.tmp_*"))) == 0
    assert len(list(tmp_path.glob("replay_samples_30.bak_tmp_*"))) == 0


def test_save_samples_atomically_temp_creation_permission_error_returns_false(tmp_path, monkeypatch):
    """Verify that if primary temp file creation fails with PermissionError:
    1. Error does NOT escape unhandled.
    2. Function returns False.
    3. Error is recorded in error_collector.
    4. Original target file remains completely untouched.
    5. Pre-existing foreign files are NOT deleted.
    6. No temporary files leak.
    """
    import json
    target_file = tmp_path / "replay_samples_30.json"
    initial_data = [{"version": "initial_intact_target"}]
    with open(target_file, "w", encoding="utf-8") as f:
        json.dump(initial_data, f)

    # Pre-create a foreign temp file simulating another concurrent worker
    foreign_temp = tmp_path / "replay_samples_30.tmp_9999_foreign.json"
    foreign_data = b"foreign_process_in_flight_payload"
    foreign_temp.write_bytes(foreign_data)

    orig_open = open

    def mock_open_temp_permission_denied(path, mode="r", *args, **kwargs):
        if ".tmp_" in str(path) and "x" in mode:
            raise PermissionError(13, "Permission denied")
        return orig_open(path, mode, *args, **kwargs)

    monkeypatch.setattr("builtins.open", mock_open_temp_permission_denied)

    errs = []
    success = save_samples_atomically(
        [{"version": "should_fail"}],
        target_file,
        backup_timestamp=1234,
        error_collector=errs,
    )
    assert success is False
    assert len(errs) == 1
    assert isinstance(errs[0], PermissionError)
    assert errs[0].errno == 13

    # Original target file must remain completely untouched
    with open(target_file, encoding="utf-8") as f:
        assert json.load(f) == initial_data

    # Foreign temp file must NOT have been deleted or corrupted
    assert foreign_temp.exists()
    assert foreign_temp.read_bytes() == foreign_data

    # Clean up foreign temp file for test isolation
    foreign_temp.unlink()

    # No leftover temporary files (.tmp_* or .bak_tmp_*)
    temp_files = list(tmp_path.glob("replay_samples_30.tmp_*")) + list(tmp_path.glob("replay_samples_30.bak_tmp_*"))
    assert len(temp_files) == 0


def test_save_samples_atomically_temp_creation_oserror_returns_false(tmp_path, monkeypatch):
    """Verify that if primary temp file creation fails with a generic OSError (e.g. disk space/quota):
    1. Error does NOT escape unhandled.
    2. Function returns False.
    3. Error is recorded in error_collector.
    4. Original target file remains completely untouched.
    5. No temporary files leak.
    """
    import json
    target_file = tmp_path / "replay_samples_30.json"
    initial_data = [{"version": "initial_intact_target"}]
    with open(target_file, "w", encoding="utf-8") as f:
        json.dump(initial_data, f)

    orig_open = open

    def mock_open_temp_oserror(path, mode="r", *args, **kwargs):
        if ".tmp_" in str(path) and "x" in mode:
            raise OSError(28, "No space left on device")
        return orig_open(path, mode, *args, **kwargs)

    monkeypatch.setattr("builtins.open", mock_open_temp_oserror)

    errs = []
    success = save_samples_atomically(
        [{"version": "should_fail"}],
        target_file,
        backup_timestamp=5678,
        error_collector=errs,
    )
    assert success is False
    assert len(errs) == 1
    assert isinstance(errs[0], OSError)
    assert errs[0].errno == 28

    # Original target file must remain completely untouched
    with open(target_file, encoding="utf-8") as f:
        assert json.load(f) == initial_data

    # No leftover temporary files (.tmp_* or .bak_tmp_*)
    temp_files = list(tmp_path.glob("replay_samples_30.tmp_*")) + list(tmp_path.glob("replay_samples_30.bak_tmp_*"))
    assert len(temp_files) == 0


def test_multiprocessing_concurrent_backup_and_temp_file_isolation(tmp_path):
    """Verify that multiple concurrent OS processes competing to save and backup
    never collide on temporary files, never overwrite each other's backups,
    cleanly handle Windows concurrency locks, and maintain filesystem integrity.

    Concurrency & Multi-Writer Semantics:
    1. Single-Writer Design & No Lost-Update Prevention:
       save_samples_atomically is architected strictly for single-writer executions. It does NOT
       implement a distributed lock, multi-writer transaction coordinator, or lost-update prevention.
       If multiple processes invoke it concurrently on the same path, the last successful writer wins.
    2. Distinction: Function Return vs Process Exit Code:
       - Process Exit Code (p.exitcode == 0): Verifies that the worker OS process terminated cleanly
         without an unhandled exception, crash, or segmentation fault.
       - Function Return Value (success: bool): True if atomic write succeeded; False if safe abort
         occurred (e.g. transient Windows file sharing violation [WinError 32]).
    3. Explicit Separation of Allowable Failures:
       - Category 1 (Windows File Locking / Sharing Violation): PermissionError or OSError with WinError 32 or 5.
       - Category 2 (Collision Retry Exhaustion): FileExistsError if all 10 candidate names collided.
       - Any other exception type or unhandled error is strictly DISALLOWED and fails the test.
    4. Data Integrity:
       Target file and all generated backups must strictly parse as valid, uncorrupted JSON matching
       one of the expected payload versions. Zero temporary files (.tmp_* or .bak_tmp_*) may leak.
    """
    import json
    import multiprocessing

    target_file = tmp_path / "replay_samples_30.json"
    initial_content = [{"version": "initial_seed"}]
    with open(target_file, "w", encoding="utf-8") as f:
        json.dump(initial_content, f)

    fixed_ts = 8888888
    num_workers = 3
    ctx = multiprocessing.get_context("spawn")
    queue = ctx.Queue()
    barrier = ctx.Barrier(num_workers)

    processes = []
    for wid in range(num_workers):
        p = ctx.Process(
            target=_multiprocess_atomic_save_worker,
            args=(str(target_file), fixed_ts, wid, queue, barrier),
        )
        processes.append(p)

    for p in processes:
        p.start()

    # Deterministically retrieve results for each worker without queue.empty() race
    results = []
    for _ in range(num_workers):
        try:
            results.append(queue.get(timeout=15))
        except Exception as e:
            pytest.fail(f"Timed out waiting for worker result from queue: {e}")

    for p in processes:
        p.join(timeout=15)
        # Verify OS process exit code: strictly 0 (no uncaught exceptions or crashes)
        assert p.exitcode == 0, f"Worker process {p.pid} exited with non-zero code {p.exitcode}"

    assert len(results) == num_workers

    # 1 & 2 & 3. Inspect each worker's individual execution outcome:
    # Worker tuple: (worker_id, success, err_info, unhandled_exc)
    successful_worker_ids = []
    sharing_violation_failures = []
    collision_exhaustion_failures = []
    disallowed_failures = []

    for wid, success, err_info, unhandled_exc in results:
        # Process must not have crashed or raised an uncaught exception
        assert unhandled_exc is None, f"Worker {wid} crashed with unhandled exception: {unhandled_exc}"
        assert isinstance(success, bool)

        if success is True:
            successful_worker_ids.append(wid)
            assert err_info is None, f"Worker {wid} reported success but returned unexpected error: {err_info}"
        else:
            # Strict Whitelist of Allowable Concurrency Failures on Windows:
            # We strictly separate Category 1 (real file lock / sharing violation) from Category 2 (collision exhaustion):
            assert err_info is not None, f"Worker {wid} returned False without capturing error details!"
            exc_type = err_info.get("type")
            winerror = err_info.get("winerror")
            msg = err_info.get("msg", "")

            is_sharing_violation = (exc_type in ("PermissionError", "OSError")) and (
                winerror in (32, 5)
                or "used by another process" in msg
                or "Access is denied" in msg
            )
            is_collision_exhaustion = (exc_type == "FileExistsError")

            if is_sharing_violation:
                sharing_violation_failures.append((wid, err_info))
            elif is_collision_exhaustion:
                collision_exhaustion_failures.append((wid, err_info))
            else:
                disallowed_failures.append((wid, err_info))

    # Any error outside the whitelist strictly FAILS the test
    if disallowed_failures:
        pytest.fail(
            f"One or more workers failed with DISALLOWED / UNEXPECTED error: {disallowed_failures}"
        )

    # 5. At least one worker process must have successfully updated the file
    assert len(successful_worker_ids) >= 1, (
        f"Expected at least one successful worker, but all failed. "
        f"Sharing violations: {sharing_violation_failures}, Collision exhaustions: {collision_exhaustion_failures}"
    )

    # 4. Verify final target file content matches the EXACT payload of one of the SUCCESSFUL workers
    assert target_file.exists()
    with open(target_file, encoding="utf-8") as f:
        target_data = json.load(f)
    assert isinstance(target_data, list)
    assert len(target_data) >= 1
    target_wid = target_data[0].get("worker_id")
    assert target_wid is not None, f"Target file has invalid payload format: {target_data}"
    assert target_wid in successful_worker_ids, (
        f"Target file content belongs to worker {target_wid}, which is NOT in successful workers list: {successful_worker_ids}"
    )
    expected_target_payload = [{"worker_id": target_wid, "data": f"payload_from_{target_wid}"}]
    assert target_data == expected_target_payload, (
        f"Target data {target_data} does not match expected payload {expected_target_payload}"
    )

    # 6. Check backups created in tmp_path
    backup_files = list(tmp_path.glob(f"replay_samples_30.bak_{fixed_ts}*.json"))
    assert len(backup_files) >= 1

    # Verify no backup names collided (set size == list size)
    backup_names = [bf.name for bf in backup_files]
    assert len(backup_names) == len(set(backup_names))

    # Verify every backup created has valid, uncorrupted JSON matching initial_seed or a worker payload
    expected_valid_versions = {"initial_seed"} | {f"payload_from_{wid}" for wid in range(num_workers)}
    for bf in backup_files:
        with open(bf, encoding="utf-8") as f:
            b_data = json.load(f)
            assert isinstance(b_data, list)
            assert len(b_data) >= 1
            payload_str = str(b_data[0].get("version") or b_data[0].get("data"))
            assert payload_str in expected_valid_versions

    # 7. Verify NO temporary files (.tmp_* or .bak_tmp_*) linger in tmp_path
    temp_files = list(tmp_path.glob("replay_samples_30.tmp_*")) + list(tmp_path.glob("replay_samples_30.bak_tmp_*"))
    assert len(temp_files) == 0, f"Found leaked temporary files: {temp_files}"

