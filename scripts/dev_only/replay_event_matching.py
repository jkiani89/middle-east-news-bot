r"""Event Matching Replay and Calibration Analysis for Phase Y-14.

READ-ONLY replay of event clustering over the last 7 days to evaluate proposed
stricter EventMatcher rules:
    Current rule: total_score >= 0.60
    Proposed rule: total_score >= 0.60 AND (shared_non_country_actor >= 1 OR keywords_score >= X)

Strictly read-only: sets default_transaction_read_only = 'on' and verifies it before running.
Zero database writes, no temporary tables, zero external API calls.

Windows PowerShell execution:
    $pass = Read-Host "Enter DB password" -AsSecureString
    $bstr = [System.Runtime.InteropServices.Marshal]::SecureStringToBSTR($pass)
    $plainPass = [System.Runtime.InteropServices.Marshal]::PtrToStringAuto($bstr)
    $env:DATABASE_URL = "postgresql+asyncpg://avnadmin:${plainPass}@your-host.aivencloud.com:port/defaultdb?ssl=require"
    python scripts/dev_only/replay_event_matching.py --days 7
    Remove-Item Env:\DATABASE_URL
"""

from __future__ import annotations

import argparse
import asyncio
import difflib
import sys
import urllib.parse
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

# Ensure project root is in sys.path
_project_root = Path(__file__).resolve().parents[2]
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from sqlalchemy import select, text

from app.config import settings
from app.database.connection import SessionLocal
from app.database.models import Article, ArticleEvent, Event, Source, Summary
from app.editorial.glossary import canonicalize_approved_names
from app.editorial.liveblog import is_live_digest
from app.editorial.perspectives import extract_non_country_entities
from app.processors.event_matcher import (
    EventMatcher,
    calculate_event_similarity,
    normalize_text,
    tokenize,
)


@dataclass
class ReplayEvent:
    id: int
    title: str
    event_type: str | None
    countries: list[str]
    actors: list[str]
    location: str | None
    importance: int
    status: str
    first_seen_at: datetime
    last_updated_at: datetime
    is_live_digest: bool
    article_ids: list[int] = field(default_factory=list)


@dataclass
class ArticleRecord:
    id: int
    source_name: str
    title: str
    url: str
    collected_at: datetime
    ai_payload: dict[str, Any]
    db_event_id: int | None
    is_live_digest: bool
    is_insufficient_data: bool


def _headline_similarity_display_only(title_a: str, title_b: str) -> float:
    """Calculate string similarity for display/ranking only (NOT used in matching decisions)."""
    return round(difflib.SequenceMatcher(None, title_a or "", title_b or "").ratio(), 3)


def _has_shared_non_country_entity(actors_a: list[str], loc_a: str | None, actors_b: list[str], loc_b: str | None) -> bool:
    set_a = extract_non_country_entities({"actors": actors_a, "location": loc_a or ""})
    set_b = extract_non_country_entities({"actors": actors_b, "location": loc_b or ""})
    return bool(set_a & set_b)


async def load_articles_from_db(days: int = 7) -> tuple[list[ArticleRecord], bool]:
    """Query articles collected in the last `days` days with their linked DB event_id."""
    now_utc = datetime.now(UTC)
    start_utc = now_utc - timedelta(days=days)

    print(f"Connecting to database to fetch articles from {start_utc.isoformat()} to {now_utc.isoformat()}...")

    async with SessionLocal() as session:
        # Enforce and verify read-only transaction
        await session.execute(text("SET default_transaction_read_only = 'on'"))
        check_ro = (await session.execute(text("SHOW default_transaction_read_only"))).scalar()
        if str(check_ro).lower() != "on":
            raise RuntimeError(f"Session is NOT read-only! SHOW default_transaction_read_only={check_ro}. Aborting.")
        print(f"Verified read-only session: default_transaction_read_only = {check_ro}")

        stmt = (
            select(
                Article.id,
                Article.title,
                Article.canonical_url,
                Article.original_url,
                Article.collected_at,
                Article.ai_payload,
                Source.name,
                ArticleEvent.event_id,
            )
            .join(Source, Source.id == Article.source_id)
            .outerjoin(ArticleEvent, ArticleEvent.article_id == Article.id)
            .where(
                Article.collected_at >= start_utc,
                Article.status != "flagged",
            )
            .order_by(Article.collected_at.asc())
        )

        rows = (await session.execute(stmt)).all()

    records: list[ArticleRecord] = []
    insufficient_count = 0

    for r in rows:
        art_id, title, can_u, orig_u, coll_at, p_load, src_name, ev_id = r
        payload = p_load if isinstance(p_load, dict) else {}
        url = can_u or orig_u or ""

        # Check live digest
        is_digest = is_live_digest(url, title) or payload.get("is_live_digest") or payload.get("article_kind") == "live_digest"

        # Check insufficient data: no actors and no location
        actors = payload.get("actors") or []
        loc = payload.get("location") or ""
        is_insufficient = (not payload) or (not actors and not loc)

        if is_insufficient:
            insufficient_count += 1

        rec = ArticleRecord(
            id=art_id,
            source_name=src_name or "",
            title=title or "",
            url=url,
            collected_at=coll_at if coll_at.tzinfo else coll_at.replace(tzinfo=UTC),
            ai_payload=payload,
            db_event_id=ev_id,
            is_live_digest=bool(is_digest),
            is_insufficient_data=bool(is_insufficient),
        )
        records.append(rec)

    print(f"Loaded {len(records)} articles ({insufficient_count} with insufficient data / no actors).")
    return records, True


def run_sequential_replay(
    articles: list[ArticleRecord],
    require_strict_gate: bool = False,
    keyword_threshold_x: float = 0.25,
    time_window_hours: int = 48,
    merge_threshold: float = 0.60,
) -> tuple[dict[int, int], list[ReplayEvent], list[dict[str, Any]]]:
    """Run sequential in-memory clustering replicating EventMatcher rules.

    Returns:
        article_to_event: map of article_id -> event_id
        events: list of ReplayEvent
        decisions: list of merge decisions with component scores
    """
    next_event_id = 1
    events: list[ReplayEvent] = []
    article_to_event: dict[int, int] = {}
    decisions: list[dict[str, Any]] = []

    for art in articles:
        analysis_data = art.ai_payload or {}
        ref_time = art.collected_at
        window_start = ref_time - timedelta(hours=time_window_hours)
        window_end = ref_time + timedelta(hours=time_window_hours)

        incoming_is_digest = art.is_live_digest

        best_candidate: ReplayEvent | None = None
        best_score = 0.0
        best_sim_score: Any = None
        best_shared_actor = False

        if not incoming_is_digest:
            # Active candidate events within 48h
            active_cands = [
                ev for ev in events
                if ev.status == "active"
                and ev.first_seen_at >= window_start
                and ev.first_seen_at <= window_end
                and not ev.is_live_digest
            ]

            for cand in active_cands:
                sim = calculate_event_similarity(cand, analysis_data)
                shared_actor = _has_shared_non_country_entity(
                    cand.actors, cand.location,
                    analysis_data.get("actors") or [], analysis_data.get("location")
                )

                qualifies = sim.total_score >= merge_threshold
                if require_strict_gate:
                    # Strict condition: shared non-country actor OR raw keywords score >= X
                    gate_passed = shared_actor or (sim.keywords_score >= keyword_threshold_x)
                    qualifies = qualifies and gate_passed

                if qualifies and sim.total_score > best_score:
                    best_score = sim.total_score
                    best_candidate = cand
                    best_sim_score = sim
                    best_shared_actor = shared_actor

        if best_candidate and best_score >= merge_threshold:
            # Merge
            best_candidate.article_ids.append(art.id)
            if ref_time > best_candidate.last_updated_at:
                best_candidate.last_updated_at = ref_time
            new_imp = analysis_data.get("importance", 3) or 3
            if new_imp > best_candidate.importance:
                best_candidate.importance = new_imp

            article_to_event[art.id] = best_candidate.id
            decisions.append({
                "article_id": art.id,
                "event_id": best_candidate.id,
                "action": "merge",
                "score": best_score,
                "sim": best_sim_score,
                "shared_actor": best_shared_actor,
                "cand_id": best_candidate.id,
            })
        else:
            # Create new event
            raw_title = canonicalize_approved_names(analysis_data.get("event_title") or art.title)
            categories = analysis_data.get("categories") or []
            primary_cat = categories[0] if categories else None
            imp = int(analysis_data.get("importance", 3) or 3)
            if incoming_is_digest:
                imp = min(imp, 3)

            new_ev = ReplayEvent(
                id=next_event_id,
                title=raw_title,
                event_type=primary_cat,
                countries=[canonicalize_approved_names(c) for c in (analysis_data.get("countries") or [])],
                actors=[canonicalize_approved_names(a) for a in (analysis_data.get("actors") or [])],
                location=analysis_data.get("location"),
                importance=imp,
                status="active",
                first_seen_at=ref_time,
                last_updated_at=ref_time,
                is_live_digest=incoming_is_digest,
                article_ids=[art.id],
            )
            events.append(new_ev)
            article_to_event[art.id] = new_ev.id
            next_event_id += 1

            decisions.append({
                "article_id": art.id,
                "event_id": new_ev.id,
                "action": "create",
                "score": 0.0,
                "sim": None,
                "shared_actor": False,
                "cand_id": None,
            })

    return article_to_event, events, decisions


def analyze_replay_control(articles: list[ArticleRecord], replay_mapping: dict[int, int]) -> float:
    """Compare replay grouping with actual DB grouping."""
    total_pairs = 0
    matched_pairs = 0

    valid_articles = [a for a in articles if a.db_event_id is not None]
    n = len(valid_articles)

    # Sample pairwise comparison
    for i in range(min(n, 500)):
        for j in range(i + 1, min(n, 500)):
            a1 = valid_articles[i]
            a2 = valid_articles[j]
            same_db = (a1.db_event_id == a2.db_event_id)
            same_replay = (replay_mapping.get(a1.id) == replay_mapping.get(a2.id))

            if same_db:
                total_pairs += 1
                if same_replay:
                    matched_pairs += 1

    if total_pairs == 0:
        return 1.0
    return round(matched_pairs / total_pairs, 4)


def compute_keyword_score_distribution(articles: list[ArticleRecord]) -> dict[str, Any]:
    """Compute min, median, p90 of raw keywords_score for DB merged articles."""
    art_map = {a.id: a for a in articles}
    event_groups: dict[int, list[ArticleRecord]] = {}
    for a in articles:
        if a.db_event_id:
            event_groups.setdefault(a.db_event_id, []).append(a)

    scores_with_actor: list[float] = []
    scores_without_actor: list[float] = []

    for ev_id, group in event_groups.items():
        if len(group) < 2:
            continue
        for i in range(len(group)):
            for j in range(i + 1, len(group)):
                a1 = group[i]
                a2 = group[j]
                p1 = a1.ai_payload or {}
                p2 = a2.ai_payload or {}
                # Calculate raw keyword similarity
                toks1 = tokenize(a1.title)
                toks2 = tokenize(a2.title)
                inter = len(toks1 & toks2)
                union = len(toks1 | toks2)
                kw_sim = round(inter / union, 4) if union else 0.0

                shared = _has_shared_non_country_entity(
                    p1.get("actors") or [], p1.get("location"),
                    p2.get("actors") or [], p2.get("location")
                )
                if shared:
                    scores_with_actor.append(kw_sim)
                else:
                    scores_without_actor.append(kw_sim)

    def _stats(arr: list[float]) -> dict[str, float]:
        if not arr:
            return {"min": 0.0, "median": 0.0, "p90": 0.0, "count": 0}
        s = sorted(arr)
        n = len(s)
        return {
            "min": s[0],
            "median": s[n // 2],
            "p90": s[int(n * 0.9)],
            "count": n,
        }

    return {
        "with_shared_actors": _stats(scores_with_actor),
        "without_shared_actors": _stats(scores_without_actor),
    }


def find_chaining_cases(events: list[ReplayEvent], articles_map: dict[int, ArticleRecord]) -> int:
    """Count multi-article events where chaining occurred (A-B and B-C merged, but sim(A, C) < 0.60)."""
    chaining_count = 0
    for ev in events:
        if len(ev.article_ids) < 3:
            continue
        arts = [articles_map[aid] for aid in ev.article_ids if aid in articles_map]
        has_chaining = False
        for i in range(len(arts)):
            for j in range(i + 2, len(arts)):
                sim = calculate_event_similarity(arts[i], arts[j].ai_payload)
                if sim.total_score < 0.60:
                    has_chaining = True
                    break
            if has_chaining:
                break
        if has_chaining:
            chaining_count += 1
    return chaining_count


async def main_async(days: int) -> None:
    print("=" * 100)
    print("  PHASE Y-14: EVENT MATCHING REPLAY AND CALIBRATION ANALYSIS")
    print(f"  Target: Sequential replay over {days} days of collected articles")
    print("=" * 100)

    try:
        articles, ok = await load_articles_from_db(days=days)
    except Exception as e:
        print(f"Error loading from database: {e}")
        print("Note: If DATABASE_URL is not set or network is unreachable, please verify connection settings.")
        return

    articles_map = {a.id: a for a in articles}

    # 1. Keywords Score Distribution
    dist = compute_keyword_score_distribution(articles)
    print("\n--- 1. Raw Keywords Score Distribution for Merged Articles in DB ---")
    print(f"Articles with shared non-country actors (Count: {dist['with_shared_actors']['count']}):")
    print(f"   Min: {dist['with_shared_actors']['min']} | Median: {dist['with_shared_actors']['median']} | P90: {dist['with_shared_actors']['p90']}")
    print(f"Articles WITHOUT shared actors (Count: {dist['without_shared_actors']['count']}):")
    print(f"   Min: {dist['without_shared_actors']['min']} | Median: {dist['without_shared_actors']['median']} | P90: {dist['without_shared_actors']['p90']}")

    # Distribution-derived X value
    x_dist = dist['with_shared_actors']['median'] or 0.20

    # 2. Sequential Replay under Current Rule (Base)
    print("\n--- 2. Executing Sequential Replay: Current Rule (Merge Threshold = 0.60) ---")
    map_base, evs_base, decs_base = run_sequential_replay(articles, require_strict_gate=False)
    multi_base = [ev for ev in evs_base if len(ev.article_ids) > 1]
    chaining_base = find_chaining_cases(evs_base, articles_map)
    control_rate = analyze_replay_control(articles, map_base)

    print(f"Total resulting events: {len(evs_base)}")
    print(f"Multi-article events: {len(multi_base)}")
    print(f"Chaining cases (A-B & B-C, but sim(A,C) < 0.60): {chaining_base}")
    print(f"Control match rate vs DB event_id: {control_rate * 100:.1f}%")
    if control_rate < 0.70:
        print("   ⚠️ Note: Match rate < 70%. Differences can stem from historical merges prior to 48h or manual adjustments.")

    # 3. Test Proposed Rules for X in (0.25, 0.35, x_dist)
    x_candidates = [0.25, 0.35, x_dist]
    results_proposed = {}

    for x in x_candidates:
        print(f"\n--- 3. Executing Sequential Replay: Proposed Strict Rule with X = {x:.2f} ---")
        map_p, evs_p, decs_p = run_sequential_replay(articles, require_strict_gate=True, keyword_threshold_x=x)
        multi_p = [ev for ev in evs_p if len(ev.article_ids) > 1]
        chaining_p = find_chaining_cases(evs_p, articles_map)

        # Count broken events: events that were multi-article in Base but split in Proposed
        broken_events = 0
        separated_articles = 0

        for b_ev in multi_base:
            assigned_groups = {map_p[aid] for aid in b_ev.article_ids if aid in map_p}
            if len(assigned_groups) > 1:
                broken_events += 1
                separated_articles += (len(b_ev.article_ids) - 1)

        results_proposed[x] = {
            "total_events": len(evs_p),
            "multi_article_events": len(multi_p),
            "broken_events": broken_events,
            "separated_articles": separated_articles,
            "chaining_cases": chaining_p,
            "events_p": evs_p,
            "map_p": map_p,
        }

        print(f"   Total Events: {len(evs_p)} (Base: {len(evs_base)})")
        print(f"   Multi-Article Events: {len(multi_p)} (Base: {len(multi_base)})")
        print(f"   Broken Multi-Article Events: {broken_events}")
        print(f"   Separated Articles (became singleton or new groups): {separated_articles}")
        print(f"   Chaining Cases: {chaining_p}")

    # 4. Generate Samples for Manual Review (using X = 0.25)
    print("\n" + "=" * 100)
    print("  4. SAMPLES FOR MANUAL REVIEW (Proposed Rule X = 0.25)")
    print("=" * 100)

    p_eval = results_proposed[0.25]
    map_p = p_eval["map_p"]

    # Collect broken pairs
    broken_pairs: list[dict[str, Any]] = []
    retained_pairs: list[dict[str, Any]] = []

    for b_ev in multi_base:
        b_arts = [articles_map[aid] for aid in b_ev.article_ids if aid in articles_map]
        for i in range(len(b_arts)):
            for j in range(i + 1, len(b_arts)):
                a1 = b_arts[i]
                a2 = b_arts[j]
                sim = calculate_event_similarity(a1, a2.ai_payload)
                h_sim = _headline_similarity_display_only(a1.title, a2.title)

                pair_info = {
                    "base_ev_id": b_ev.id,
                    "art1": a1,
                    "art2": a2,
                    "sim": sim,
                    "headline_sim": h_sim,
                }
                if map_p[a1.id] != map_p[a2.id]:
                    broken_pairs.append(pair_info)
                else:
                    retained_pairs.append(pair_info)

    # Sort broken pairs by headline similarity
    broken_sorted = sorted(broken_pairs, key=lambda x: x["headline_sim"])
    # 10 lowest headline sim (likely correct separations)
    ten_lowest_broken = broken_sorted[:10]
    # 10 highest headline sim (likely false splits / duplicate news)
    ten_highest_broken = sorted(broken_sorted[-10:], key=lambda x: x["headline_sim"], reverse=True) if len(broken_sorted) >= 10 else []

    # 10 lowest score retained pairs (borderline merges)
    retained_sorted = sorted(retained_pairs, key=lambda x: x["sim"].total_score)
    ten_borderline_retained = retained_sorted[:10]

    def _print_sample_pair(idx: int, p: dict[str, Any], tag: str):
        a1 = p["art1"]
        a2 = p["art2"]
        sim = p["sim"]
        h_sim = p["headline_sim"]
        print(f"Sample #{idx} [{tag}] — Base Event #{p['base_ev_id']}")
        print(f"   Article 1 (#{a1.id} - {a1.source_name}): {a1.title[:75]}")
        print(f"      Actors: {a1.ai_payload.get('actors') or []} | Loc: {a1.ai_payload.get('location') or '-'}")
        print(f"   Article 2 (#{a2.id} - {a2.source_name}): {a2.title[:75]}")
        print(f"      Actors: {a2.ai_payload.get('actors') or []} | Loc: {a2.ai_payload.get('location') or '-'}")
        print(f"   Component Scores: Countries={sim.countries_score} | Actors={sim.actors_score} | Cat={sim.category_score} | Loc={sim.location_score} | KW={sim.keywords_score}")
        print(f"   Total Score: {sim.total_score:.3f} | Headline Sim [display-only]: {h_sim:.3f}")
        print("-" * 90)

    print("\n--- [A] 10 Broken Pairs with Lowest Headline Similarity (Likely Correct Separations) ---")
    for i, p in enumerate(ten_lowest_broken, 1):
        _print_sample_pair(i, p, "Correct Separation Candidate")

    print("\n--- [B] 10 Broken Pairs with Highest Headline Similarity (Risk of Duplicate News) ---")
    for i, p in enumerate(ten_highest_broken, 1):
        _print_sample_pair(i, p, "Risk of False Split Candidate")

    print("\n--- [C] 10 Borderline Merged Pairs in Both Rules (Lowest Merge Scores) ---")
    for i, p in enumerate(ten_borderline_retained, 1):
        _print_sample_pair(i, p, "Retained Borderline Merge")


def main() -> None:
    parser = argparse.ArgumentParser(description="Replay event matching and calibrate proposed rules.")
    parser.add_argument("--days", type=int, default=7, help="Number of days to replay (default: 7)")
    args = parser.parse_args()

    asyncio.run(main_async(args.days))


if __name__ == "__main__":
    main()
