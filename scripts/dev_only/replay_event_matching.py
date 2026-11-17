r"""Event Matching Replay and Calibration Analysis for Phase Y-14.

READ-ONLY replay of event clustering over the last N days to evaluate proposed
stricter EventMatcher rules:
    Current rule: total_score >= 0.60
    Proposed rule: total_score >= 0.60 AND (shared_non_country_actor >= 1 OR keywords_score >= X)

Strictly read-only: executes SET TRANSACTION READ ONLY in begin() and verifies transaction_read_only = 'on'.
Zero database writes, no temporary tables, zero external API calls, zero model calls.

Windows PowerShell execution:
    $securePass = Read-Host "Enter Aiven DB password" -AsSecureString
    $bstr = [System.Runtime.InteropServices.Marshal]::SecureStringToBSTR($securePass)
    $plainPass = [System.Runtime.InteropServices.Marshal]::PtrToStringAuto($bstr)
    $env:DATABASE_URL = "postgresql+asyncpg://avnadmin:${plainPass}@pg-2f0e4f1c-newsbot-db.e.aivencloud.com:27362/defaultdb?ssl=require"
    .venv\Scripts\python.exe scripts/dev_only/replay_event_matching.py --days 7
    Remove-Item Env:\DATABASE_URL
    [System.Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
"""

from __future__ import annotations

import argparse
import asyncio
import difflib
import sys
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
from app.database.models import Article, ArticleEvent, Event, Source
from app.editorial.glossary import canonicalize_approved_names
from app.editorial.liveblog import is_live_digest
from app.editorial.perspectives import extract_non_country_entities
from app.processors.event_matcher import (
    calculate_event_similarity,
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
    is_follow_up: bool = False
    parent_event_id: int | None = None
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


@dataclass
class EventCandidateCompat:
    """Adapter ensuring calculate_event_similarity can inspect candidate attributes without AttributeError."""
    title: str
    event_type: str | None
    countries: list[str]
    actors: list[str]
    location: str | None


def _to_event_candidate(item: ArticleRecord | ReplayEvent | Event) -> Any:
    """Convert an ArticleRecord, ReplayEvent, or Event into a calculate_event_similarity-compatible candidate."""
    if isinstance(item, (Event, ReplayEvent)):
        return item
    payload = getattr(item, "ai_payload", {}) or {}
    raw_title = payload.get("event_title") or payload.get("headline_fa") or getattr(item, "title", "")
    return EventCandidateCompat(
        title=canonicalize_approved_names(raw_title),
        event_type=(payload.get("categories") or [None])[0],
        countries=payload.get("countries") or [],
        actors=payload.get("actors") or [],
        location=payload.get("location"),
    )


def _headline_similarity_display_only(title_a: str, title_b: str) -> float:
    """Calculate string similarity for display/ranking only (NOT used in matching decisions)."""
    return round(difflib.SequenceMatcher(None, title_a or "", title_b or "").ratio(), 3)


def _has_shared_non_country_entity(
    actors_a: list[str],
    loc_a: str | None,
    actors_b: list[str],
    loc_b: str | None,
) -> bool:
    set_a = extract_non_country_entities({"actors": actors_a, "location": loc_a or ""})
    set_b = extract_non_country_entities({"actors": actors_b, "location": loc_b or ""})
    return bool(set_a & set_b)


async def load_articles_from_db(days: int = 7) -> tuple[list[ArticleRecord], bool]:
    """Query articles collected in the last `days` days with strictly read-only transaction."""
    now_utc = datetime.now(UTC)
    start_utc = now_utc - timedelta(days=days)

    print(f"Connecting to database to fetch articles from {start_utc.isoformat()} to {now_utc.isoformat()}...")

    async with SessionLocal() as session:
        # Enforce and verify read-only transaction at transaction start
        async with session.begin():
            await session.execute(text("SET TRANSACTION READ ONLY"))
            check_ro = (await session.execute(text("SHOW transaction_read_only"))).scalar()
            if str(check_ro).lower() != "on":
                raise RuntimeError(f"Session is NOT read-only! SHOW transaction_read_only={check_ro}. Aborting.")
            print(f"Verified read-only transaction: transaction_read_only = {check_ro}")

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
    empty_actor_count = 0
    empty_location_count = 0
    both_empty_count = 0

    for r in rows:
        art_id, title, can_u, orig_u, coll_at, p_load, src_name, ev_id = r
        payload = p_load if isinstance(p_load, dict) else {}
        url = can_u or orig_u or ""

        # Check live digest
        is_digest = is_live_digest(url, title) or payload.get("is_live_digest") or payload.get("article_kind") == "live_digest"

        # Check empty actors and empty location independently
        actors = payload.get("actors") or []
        loc = str(payload.get("location") or "").strip()
        has_actors = bool(actors)
        has_location = bool(loc)

        if not has_actors:
            empty_actor_count += 1
        if not has_location:
            empty_location_count += 1
        if not has_actors and not has_location:
            both_empty_count += 1

        rec = ArticleRecord(
            id=art_id,
            source_name=src_name or "",
            title=title or "",
            url=url,
            collected_at=coll_at if coll_at.tzinfo else coll_at.replace(tzinfo=UTC),
            ai_payload=payload,
            db_event_id=ev_id,
            is_live_digest=bool(is_digest),
            is_insufficient_data=(not has_actors and not has_location),
        )
        records.append(rec)

    print(f"Loaded {len(records)} articles across time window:")
    print(f"   • Total Articles Analyzed: {len(records)}")
    print(f"   • Articles with missing actors (Empty Actors): {empty_actor_count}")
    print(f"   • Articles with missing location (Empty Location): {empty_location_count}")
    print(f"   • Articles with BOTH missing (Insufficient Data): {both_empty_count}")
    return records, True


def run_sequential_replay(
    articles: list[ArticleRecord],
    require_strict_gate: bool = False,
    keyword_threshold_x: float = 0.25,
    time_window_hours: int = 48,
    follow_up_window_days: int = 14,
    merge_threshold: float = 0.60,
    follow_up_threshold: float = 0.80,
) -> tuple[dict[int, int], list[ReplayEvent], list[dict[str, Any]]]:
    """Run sequential in-memory clustering replicating 100% of EventMatcher rules.

    Replicates both operational paths:
        Path 1: Active events within 48h (threshold >= 0.60)
        Path 2: Historical Follow-up events (48h < t <= 14d, threshold >= 0.80)

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

        # 1. Active candidate events within 48h
        best_candidate: ReplayEvent | None = None
        best_score = 0.0
        best_sim_score: Any = None
        best_shared_actor = False

        if not incoming_is_digest:
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
                    gate_passed = shared_actor or (sim.keywords_score >= keyword_threshold_x)
                    qualifies = qualifies and gate_passed

                if qualifies and sim.total_score > best_score:
                    best_score = sim.total_score
                    best_candidate = cand
                    best_sim_score = sim
                    best_shared_actor = shared_actor

        # 2. Historical Follow-up candidate (>48h and <= follow_up_window_days)
        historical_start = ref_time - timedelta(days=follow_up_window_days)
        best_hist_event: ReplayEvent | None = None
        best_hist_score = 0.0
        best_hist_sim: Any = None
        best_hist_shared_actor = False

        if not incoming_is_digest and (best_candidate is None or best_score < merge_threshold):
            hist_cands = [
                ev for ev in events
                if ev.status == "active"
                and ev.first_seen_at >= historical_start
                and ev.first_seen_at < window_start
                and not ev.is_live_digest
            ]
            for cand in hist_cands:
                sim = calculate_event_similarity(cand, analysis_data)
                shared_actor = _has_shared_non_country_entity(
                    cand.actors, cand.location,
                    analysis_data.get("actors") or [], analysis_data.get("location")
                )
                qualifies = sim.total_score >= follow_up_threshold
                if require_strict_gate:
                    gate_passed = shared_actor or (sim.keywords_score >= keyword_threshold_x)
                    qualifies = qualifies and gate_passed

                if qualifies and sim.total_score > best_hist_score:
                    best_hist_score = sim.total_score
                    best_hist_event = cand
                    best_hist_sim = sim
                    best_hist_shared_actor = shared_actor

        if best_candidate and best_score >= merge_threshold:
            # Action: Merge into active event
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
        elif best_hist_event and best_hist_score >= follow_up_threshold:
            # Action: Create Follow-up event
            raw_title = canonicalize_approved_names(analysis_data.get("event_title") or art.title)
            follow_up_title = f"[پیگیری] {raw_title}"
            categories = analysis_data.get("categories") or []
            primary_cat = categories[0] if categories else best_hist_event.event_type

            follow_up_ev = ReplayEvent(
                id=next_event_id,
                title=follow_up_title,
                event_type=primary_cat,
                countries=[canonicalize_approved_names(c) for c in (analysis_data.get("countries") or best_hist_event.countries)],
                actors=[canonicalize_approved_names(a) for a in (analysis_data.get("actors") or best_hist_event.actors)],
                location=analysis_data.get("location") or best_hist_event.location,
                importance=analysis_data.get("importance", 3) or 3,
                status="active",
                first_seen_at=ref_time,
                last_updated_at=ref_time,
                is_live_digest=False,
                is_follow_up=True,
                parent_event_id=best_hist_event.id,
                article_ids=[art.id],
            )
            events.append(follow_up_ev)
            article_to_event[art.id] = follow_up_ev.id
            next_event_id += 1

            decisions.append({
                "article_id": art.id,
                "event_id": follow_up_ev.id,
                "action": "follow_up",
                "score": best_hist_score,
                "sim": best_hist_sim,
                "shared_actor": best_hist_shared_actor,
                "cand_id": best_hist_event.id,
            })
        else:
            # Action: Create initial new event
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


def analyze_replay_control(
    articles: list[ArticleRecord],
    replay_mapping: dict[int, int],
) -> dict[str, float]:
    """Evaluate pairwise clustering metrics (Precision, Recall, F1, FPR, FNR) vs DB grouping."""
    valid_articles = [a for a in articles if a.db_event_id is not None and a.id in replay_mapping]
    n = len(valid_articles)

    tp = 0
    fp = 0
    fn = 0
    tn = 0

    for i in range(n):
        for j in range(i + 1, n):
            a1 = valid_articles[i]
            a2 = valid_articles[j]
            same_db = (a1.db_event_id == a2.db_event_id)
            same_replay = (replay_mapping[a1.id] == replay_mapping[a2.id])

            if same_db and same_replay:
                tp += 1
            elif not same_db and same_replay:
                fp += 1
            elif same_db and not same_replay:
                fn += 1
            else:
                tn += 1

    precision = round(tp / (tp + fp), 4) if (tp + fp) > 0 else 1.0
    recall = round(tp / (tp + fn), 4) if (tp + fn) > 0 else 1.0
    f1 = round(2 * precision * recall / (precision + recall), 4) if (precision + recall) > 0 else 0.0
    fpr = round(fp / (fp + tn), 4) if (fp + tn) > 0 else 0.0
    fnr = round(fn / (tp + fn), 4) if (tp + fn) > 0 else 0.0

    return {
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "tn": tn,
        "total_pairs": tp + fp + fn + tn,
        "precision": precision,
        "recall": recall,
        "f1": f1,
        "fpr": fpr,
        "fnr": fnr,
    }


def compute_keyword_score_distribution(articles: list[ArticleRecord]) -> dict[str, Any]:
    """Compute min, median, p90 of raw keywords_score for DB merged articles using exact EventMatcher logic."""
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

                # Exact EventMatcher keyword comparison: candidate title vs incoming event_title/headline_fa
                t1 = p1.get("event_title") or p1.get("headline_fa") or a1.title or ""
                t2 = p2.get("event_title") or p2.get("headline_fa") or a2.title or ""
                toks1 = tokenize(t1)
                toks2 = tokenize(t2)
                inter = len(toks1 & toks2)
                union = len(toks1 | toks2)
                kw_sim = round(inter / union, 4) if union else 0.0

                shared = _has_shared_non_country_entity(
                    p1.get("actors") or [], p1.get("location"),
                    p2.get("actors") or [], p2.get("location"),
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
                sim = calculate_event_similarity(_to_event_candidate(arts[i]), arts[j].ai_payload)
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
    print("  PHASE Y-14: EVENT MATCHING REPLAY AND CALIBRATION ANALYSIS (AUDITED)")
    print(f"  Target: Sequential replay over {days} days of collected articles")
    print("=" * 100)

    try:
        articles, ok = await load_articles_from_db(days=days)
    except Exception as e:
        print(f"Error loading from database: {e}")
        print("Note: If DATABASE_URL is not set or network is unreachable, please verify connection settings.")
        return

    articles_map = {a.id: a for a in articles}

    # 1. Keywords Score Distribution using EventMatcher logic
    dist = compute_keyword_score_distribution(articles)
    print("\n--- 1. Raw Keywords Score Distribution for Merged Articles in DB ---")
    print(f"Articles with shared non-country actors (Count: {dist['with_shared_actors']['count']}):")
    print(f"   Min: {dist['with_shared_actors']['min']} | Median: {dist['with_shared_actors']['median']} | P90: {dist['with_shared_actors']['p90']}")
    print(f"Articles WITHOUT shared actors (Count: {dist['without_shared_actors']['count']}):")
    print(f"   Min: {dist['without_shared_actors']['min']} | Median: {dist['without_shared_actors']['median']} | P90: {dist['without_shared_actors']['p90']}")

    x_dist = dist["with_shared_actors"]["median"] or 0.20

    # 2. Sequential Replay under Current Rule (Base: Merge Threshold = 0.60)
    print("\n--- 2. Executing Sequential Replay: Current Rule (Merge Threshold = 0.60, Follow-Up = 0.80) ---")
    map_base, evs_base, decs_base = run_sequential_replay(articles, require_strict_gate=False)
    multi_base = [ev for ev in evs_base if len(ev.article_ids) > 1]
    follow_up_base = [ev for ev in evs_base if ev.is_follow_up]
    chaining_base = find_chaining_cases(evs_base, articles_map)
    ctrl_base = analyze_replay_control(articles, map_base)

    print(f"Total resulting events: {len(evs_base)}")
    print(f"Multi-article events: {len(multi_base)}")
    print(f"Follow-up events created (Historical >= 0.80): {len(follow_up_base)}")
    print(f"Chaining cases (A-B & B-C, but sim(A,C) < 0.60): {chaining_base}")
    print(f"Pairwise Agreement vs DB Clustering (توافق دوبه‌دو با دیتابیس):")
    print(f"   • Agreement-Precision: {ctrl_base['precision']:.4f}")
    print(f"   • Agreement-Recall: {ctrl_base['recall']:.4f}")
    print(f"   • Agreement-F1: {ctrl_base['f1']:.4f}")
    print(f"   • False Positive Rate: {ctrl_base['fpr']:.4f}")
    print("   [توجه روش‌شناختی: این معیارها صرفاً توافق با خوشه‌بندی ثبت‌شده در دیتابیس هستند، نه دقت مطلق واقعی]")

    # 3. Test Proposed Rules for X in (0.25, 0.35, x_dist)
    x_candidates = [0.25, 0.35, x_dist]
    results_proposed: dict[float, Any] = {}

    for x in x_candidates:
        print(f"\n--- 3. Executing Sequential Replay: Proposed Strict Rule with X = {x:.2f} ---")
        map_p, evs_p, decs_p = run_sequential_replay(articles, require_strict_gate=True, keyword_threshold_x=x)
        multi_p = [ev for ev in evs_p if len(ev.article_ids) > 1]
        follow_up_p = [ev for ev in evs_p if ev.is_follow_up]
        chaining_p = find_chaining_cases(evs_p, articles_map)
        ctrl_p = analyze_replay_control(articles, map_p)

        # Rigorous split metrics
        broken_events = 0
        separated_articles = 0
        singleton_separated = 0
        resulting_groups_count = 0

        for b_ev in multi_base:
            assigned_groups: dict[int, list[int]] = {}
            for aid in b_ev.article_ids:
                if aid in map_p:
                    assigned_groups.setdefault(map_p[aid], []).append(aid)

            if len(assigned_groups) > 1:
                broken_events += 1
                resulting_groups_count += len(assigned_groups)
                sizes = sorted([len(g) for g in assigned_groups.values()], reverse=True)
                separated_articles += (len(b_ev.article_ids) - sizes[0])
                singleton_separated += sum(1 for s in sizes if s == 1)

        results_proposed[x] = {
            "total_events": len(evs_p),
            "multi_article_events": len(multi_p),
            "follow_up_events": len(follow_up_p),
            "broken_events": broken_events,
            "resulting_groups": resulting_groups_count,
            "separated_articles": separated_articles,
            "singleton_separated": singleton_separated,
            "chaining_cases": chaining_p,
            "ctrl": ctrl_p,
            "events_p": evs_p,
            "map_p": map_p,
        }

        print(f"   • Total Events: {len(evs_p)} (Base: {len(evs_base)})")
        print(f"   • Multi-Article Events: {len(multi_p)} (Base: {len(multi_base)})")
        print(f"   • Follow-Up Events: {len(follow_up_p)} (Base: {len(follow_up_base)})")
        print(f"   • Broken Multi-Article Events (رویدادهای شکسته): {broken_events}")
        print(f"   • Resulting Groups from Split (گروه‌های حاصل از شکست): {resulting_groups_count}")
        print(f"   • Separated Articles from Core (مقالات جداشده از خوشه اصلی): {separated_articles}")
        print(f"   • Singleton Separated Articles (مقالات تفکیک‌شده تک‌عضوی): {singleton_separated}")
        print(f"   • Chaining Cases (موارد زنجیره‌سازی): {chaining_p}")
        print(f"   • Pairwise Agreement vs DB Clustering:")
        print(f"        Agreement-Precision: {ctrl_p['precision']:.4f} | Agreement-Recall: {ctrl_p['recall']:.4f} | Agreement-F1: {ctrl_p['f1']:.4f} | FPR: {ctrl_p['fpr']:.4f}")

    # Comparative Summary Table
    print("\n" + "=" * 100)
    print("  خلاصه مقایسه‌ای اجرای بازپخش: قاعده فعلی در برابر قواعد پیشنهادی")
    print("=" * 100)
    print(f"{'شاخص / متریک':<40} | {'Base (0.60)':<15} | {'Prop (X=0.25)':<15} | {'Prop (X=0.35)':<15}")
    print("-" * 95)
    print(f"{'تعداد کل رویدادها (Total Events)':<40} | {len(evs_base):<15} | {results_proposed[0.25]['total_events']:<15} | {results_proposed[0.35]['total_events']:<15}")
    print(f"{'رویدادهای چندمقاله‌ای (Multi-Article)':<40} | {len(multi_base):<15} | {results_proposed[0.25]['multi_article_events']:<15} | {results_proposed[0.35]['multi_article_events']:<15}")
    print(f"{'رویدادهای پیگیری (Follow-Up Events)':<40} | {len(follow_up_base):<15} | {results_proposed[0.25]['follow_up_events']:<15} | {results_proposed[0.35]['follow_up_events']:<15}")
    print(f"{'رویدادهای شکسته (Broken Events)':<40} | {'—':<15} | {results_proposed[0.25]['broken_events']:<15} | {results_proposed[0.35]['broken_events']:<15}")
    print(f"{'گروه‌های حاصل از شکست (Resulting Groups)':<40} | {'—':<15} | {results_proposed[0.25]['resulting_groups']:<15} | {results_proposed[0.35]['resulting_groups']:<15}")
    print(f"{'مقالات جداشده از هسته (Separated Articles)':<40} | {'—':<15} | {results_proposed[0.25]['separated_articles']:<15} | {results_proposed[0.35]['separated_articles']:<15}")
    print(f"{'مقالات تک‌عضوی جدید (Singleton Articles)':<40} | {'—':<15} | {results_proposed[0.25]['singleton_separated']:<15} | {results_proposed[0.35]['singleton_separated']:<15}")
    print(f"{'موارد زنجیره‌سازی (Chaining Cases)':<40} | {chaining_base:<15} | {results_proposed[0.25]['chaining_cases']:<15} | {results_proposed[0.35]['chaining_cases']:<15}")
    print(f"{'توافق دقت با دیتابیس (DB Agreement-P)':<40} | {ctrl_base['precision']:<15.4f} | {results_proposed[0.25]['ctrl']['precision']:<15.4f} | {results_proposed[0.35]['ctrl']['precision']:<15.4f}")
    print(f"{'توافق پوشش با دیتابیس (DB Agreement-R)':<40} | {ctrl_base['recall']:<15.4f} | {results_proposed[0.25]['ctrl']['recall']:<15.4f} | {results_proposed[0.35]['ctrl']['recall']:<15.4f}")
    print(f"{'F1 توافق با دیتابیس (DB Agreement-F1)':<40} | {ctrl_base['f1']:<15.4f} | {results_proposed[0.25]['ctrl']['f1']:<15.4f} | {results_proposed[0.35]['ctrl']['f1']:<15.4f}")
    print("=" * 100)

    # 4. Generate Samples for Manual Review (using X = 0.25)
    print("\n" + "=" * 100)
    print("  4. SAMPLES FOR MANUAL REVIEW (Proposed Rule X = 0.25)")
    print("=" * 100)

    p_eval = results_proposed[0.25]
    map_p = p_eval["map_p"]

    broken_pairs: list[dict[str, Any]] = []
    retained_pairs: list[dict[str, Any]] = []

    for b_ev in multi_base:
        b_arts = [articles_map[aid] for aid in b_ev.article_ids if aid in articles_map]
        for i in range(len(b_arts)):
            for j in range(i + 1, len(b_arts)):
                a1 = b_arts[i]
                a2 = b_arts[j]
                sim = calculate_event_similarity(_to_event_candidate(a1), a2.ai_payload)
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

    broken_sorted = sorted(broken_pairs, key=lambda x: x["headline_sim"])
    ten_lowest_broken = broken_sorted[:10]
    ten_highest_broken = sorted(broken_sorted[-10:], key=lambda x: x["headline_sim"], reverse=True) if len(broken_sorted) >= 10 else []

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
    parser.add_argument("--days", type=int, default=30, help="Number of days to replay (default: 30)")
    args = parser.parse_args()

    asyncio.run(main_async(args.days))


if __name__ == "__main__":
    main()
