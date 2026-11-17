"""Unit tests for Y-14 event matching replay and calibration analysis."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.processors.event_matcher import calculate_event_similarity
from scripts.dev_only.replay_event_matching import (
    ArticleRecord,
    EventCandidateCompat,
    _headline_similarity_display_only,
    _to_event_candidate,
    analyze_replay_control,
    compute_keyword_score_distribution,
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
