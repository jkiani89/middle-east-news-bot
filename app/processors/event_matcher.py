"""Event Matching and Clustering Engine for Phase 5.

Implements dynamic weight redistribution, bootstrap guardrails for missing actors,
48-hour active clustering window, and follow-up event linking (>48h) via parent_event_id.
Strictly adheres to docs/PHASE_5_DESIGN.md.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import Article, Event, Summary

logger = logging.getLogger(__name__)

STOPWORDS = {
    "the", "in", "on", "at", "and", "a", "an", "of", "for", "to", "with",
    "from", "by", "is", "are", "was", "were", "be", "been", "being",
    "have", "has", "had", "do", "does", "did", "but", "if", "or", "because",
    "as", "until", "while", "that", "this", "these", "those", "about", "after"
}


def _utcnow() -> datetime:
    return datetime.now(UTC)


def normalize_text(text: str | None) -> str:
    if not text:
        return ""
    text = text.lower()
    text = re.sub(r"[^\w\s]", " ", text)
    return " ".join(text.split())


def tokenize(text: str | None) -> set[str]:
    if not text:
        return set()
    cleaned = normalize_text(text)
    words = cleaned.split()
    return {w for w in words if len(w) > 2 and w not in STOPWORDS}


def jaccard_similarity(set_a: set[str], set_b: set[str]) -> float:
    if not set_a or not set_b:
        return 0.0
    intersection = len(set_a & set_b)
    union = len(set_a | set_b)
    if union == 0:
        return 0.0
    return intersection / union


@dataclass
class SimilarityScore:
    total_score: float
    countries_score: float
    actors_score: float
    category_score: float
    location_score: float
    keywords_score: float
    distinct_matches: int
    weights_applied: dict[str, float]


def calculate_event_similarity(
    event: Event,
    incoming_data: dict[str, Any],
) -> SimilarityScore:
    """Calculate deterministic similarity between an existing Event and an incoming article's AI analysis.

    Implements the Dynamic Weight Redistribution Matrix from PHASE_5_DESIGN.md:
    - Both have actors: countries=0.25, actors=0.25, category=0.20, location=0.15, keywords=0.15
    - Only one has actors: countries=0.35, actors=0.00, category=0.25, location=0.20, keywords=0.20
    - Neither has actors: countries=0.40, actors=0.00, category=0.25, location=0.20, keywords=0.15
    """
    ev_countries = {normalize_text(c) for c in (event.countries or []) if c}
    inc_countries = {normalize_text(c) for c in (incoming_data.get("countries") or []) if c}
    s_countries = jaccard_similarity(ev_countries, inc_countries)

    ev_actors = {normalize_text(a) for a in (event.actors or []) if a}
    inc_actors = {normalize_text(a) for a in (incoming_data.get("actors") or []) if a}
    has_ev_actors = bool(ev_actors)
    has_inc_actors = bool(inc_actors)

    if has_ev_actors and has_inc_actors:
        s_actors = jaccard_similarity(ev_actors, inc_actors)
    else:
        s_actors = 0.0

    # Category matching
    ev_cat = (event.event_type or "").strip().upper()
    inc_categories = [c.strip().upper() for c in (incoming_data.get("categories") or []) if c]
    inc_primary_cat = inc_categories[0] if inc_categories else ""

    if ev_cat and (ev_cat == inc_primary_cat or ev_cat in inc_categories):
        s_category = 1.0
    elif not ev_cat and not inc_categories:
        s_category = 0.5
    else:
        s_category = 0.0

    # Location matching
    ev_loc = normalize_text(event.location)
    inc_loc = normalize_text(incoming_data.get("location"))
    if ev_loc and inc_loc:
        if ev_loc == inc_loc:
            s_location = 1.0
        else:
            s_location = jaccard_similarity(tokenize(ev_loc), tokenize(inc_loc))
    elif not ev_loc and not inc_loc:
        s_location = 0.5
    else:
        s_location = 0.0

    # Keyword matching (Title / Event Title)
    ev_tokens = tokenize(event.title)
    inc_title = incoming_data.get("event_title") or incoming_data.get("title") or ""
    inc_tokens = tokenize(inc_title)
    s_keywords = jaccard_similarity(ev_tokens, inc_tokens)

    # Dynamic Weight Assignment
    if has_ev_actors and has_inc_actors:
        weights = {"countries": 0.25, "actors": 0.25, "category": 0.20, "location": 0.15, "keywords": 0.15}
    elif has_ev_actors or has_inc_actors:
        weights = {"countries": 0.35, "actors": 0.00, "category": 0.25, "location": 0.20, "keywords": 0.20}
    else:
        weights = {"countries": 0.40, "actors": 0.00, "category": 0.25, "location": 0.20, "keywords": 0.15}

    raw_score = (
        weights["countries"] * s_countries
        + weights["actors"] * s_actors
        + weights["category"] * s_category
        + weights["location"] * s_location
        + weights["keywords"] * s_keywords
    )

    # Count distinct feature matches (for Bootstrap Guardrail)
    distinct_matches = 0
    if s_countries > 0.0:
        distinct_matches += 1
    if s_actors > 0.0:
        distinct_matches += 1
    if s_category >= 1.0:
        distinct_matches += 1
    if s_location > 0.3:
        distinct_matches += 1
    if s_keywords > 0.2:
        distinct_matches += 1

    # Bootstrap Security Guardrail:
    # If actors are missing on either or both sides, at least TWO distinct features must match!
    # A single matching category or generic single country match must NOT trigger a false merge.
    final_score = raw_score
    if (not has_ev_actors or not has_inc_actors) and distinct_matches < 2:
        final_score = min(raw_score, 0.39)

    return SimilarityScore(
        total_score=round(final_score, 4),
        countries_score=round(s_countries, 4),
        actors_score=round(s_actors, 4),
        category_score=round(s_category, 4),
        location_score=round(s_location, 4),
        keywords_score=round(s_keywords, 4),
        distinct_matches=distinct_matches,
        weights_applied=weights,
    )


@dataclass
class EventMatchResult:
    event_id: int
    match_type: str  # "initial", "auto", "follow_up"
    confidence: float
    is_new_event: bool
    event: Event


class EventMatcher:
    """Clustering engine for Middle East events."""

    def __init__(
        self,
        time_window_hours: int = 48,
        follow_up_window_days: int = 14,
        merge_threshold: float = 0.70,
        follow_up_threshold: float = 0.80,
    ):
        self.time_window_hours = time_window_hours
        self.follow_up_window_days = follow_up_window_days
        self.merge_threshold = merge_threshold
        self.follow_up_threshold = follow_up_threshold

    async def match_or_create_event(
        self,
        session: AsyncSession,
        article: Article,
        analysis_data: dict[str, Any],
        source_name: str = "",
    ) -> EventMatchResult:
        """Evaluate incoming article against active and historical events to merge, follow-up, or create new."""
        ref_time = article.collected_at or _utcnow()
        if ref_time.tzinfo is None:
            ref_time = ref_time.replace(tzinfo=UTC)

        window_start = ref_time - timedelta(hours=self.time_window_hours)
        window_end = ref_time + timedelta(hours=self.time_window_hours)

        # 1. Query candidate active events within the 48-hour window
        stmt_active = select(Event).where(
            Event.status == "active",
            Event.first_seen_at >= window_start,
            Event.first_seen_at <= window_end,
        ).order_by(Event.first_seen_at.desc())

        active_events = (await session.scalars(stmt_active)).all()

        best_active_event: Event | None = None
        best_active_score = 0.0

        for candidate in active_events:
            sim = calculate_event_similarity(candidate, analysis_data)
            if sim.total_score > best_active_score:
                best_active_score = sim.total_score
                best_active_event = candidate

        # Check if active candidate qualifies for automatic merge
        if best_active_event and best_active_score >= self.merge_threshold:
            logger.info(
                "Merging Article #%d into Event #%d ('%s') with confidence %.3f",
                article.id,
                best_active_event.id,
                best_active_event.title[:40],
                best_active_score,
            )
            # Update last_updated_at and importance if escalated
            if ref_time > (best_active_event.last_updated_at or best_active_event.first_seen_at):
                best_active_event.last_updated_at = ref_time

            new_importance = analysis_data.get("importance", 3)
            if new_importance > best_active_event.importance:
                best_active_event.importance = new_importance

            # Apply Summary Staleness Policy Option C:
            # Append source metadata to existing summary details_fa
            stmt_summary = select(Summary).where(Summary.event_id == best_active_event.id)
            existing_summary = (await session.scalars(stmt_summary)).first()
            if existing_summary:
                details = dict(existing_summary.details_fa or {})
                sources = list(details.get("sources", []))
                new_source_entry = {
                    "source": source_name,
                    "url": article.original_url,
                    "article_id": article.id,
                    "added_at": ref_time.isoformat(),
                }
                if new_source_entry not in sources:
                    sources.append(new_source_entry)
                    details["sources"] = sources
                    existing_summary.details_fa = details

            return EventMatchResult(
                event_id=best_active_event.id,
                match_type="auto",
                confidence=best_active_score,
                is_new_event=False,
                event=best_active_event,
            )

        # 2. Check for Follow-Up candidate (>48h and <= follow_up_window_days)
        historical_start = ref_time - timedelta(days=self.follow_up_window_days)
        stmt_hist = select(Event).where(
            Event.first_seen_at >= historical_start,
            Event.first_seen_at < window_start,
        ).order_by(Event.first_seen_at.desc())

        hist_events = (await session.scalars(stmt_hist)).all()
        best_hist_event: Event | None = None
        best_hist_score = 0.0

        for candidate in hist_events:
            sim = calculate_event_similarity(candidate, analysis_data)
            if sim.total_score > best_hist_score:
                best_hist_score = sim.total_score
                best_hist_event = candidate

        if best_hist_event and best_hist_score >= self.follow_up_threshold:
            logger.info(
                "Article #%d matched historical Event #%d ('%s') with confidence %.3f > 48h. Creating follow-up event.",
                article.id,
                best_hist_event.id,
                best_hist_event.title[:40],
                best_hist_score,
            )
            raw_title = analysis_data.get("event_title") or article.title
            follow_up_title = f"[پیگیری] {raw_title}"
            categories = analysis_data.get("categories") or []
            primary_cat = categories[0] if categories else best_hist_event.event_type

            follow_up_event = Event(
                title=follow_up_title,
                event_type=primary_cat,
                countries=analysis_data.get("countries") or best_hist_event.countries,
                actors=analysis_data.get("actors") or best_hist_event.actors,
                location=analysis_data.get("location") or best_hist_event.location,
                importance=analysis_data.get("importance", 3),
                status="active",
                parent_event_id=best_hist_event.id,
                first_seen_at=ref_time,
                last_updated_at=ref_time,
            )
            session.add(follow_up_event)
            await session.flush()

            return EventMatchResult(
                event_id=follow_up_event.id,
                match_type="follow_up",
                confidence=best_hist_score,
                is_new_event=True,
                event=follow_up_event,
            )

        # 3. Create brand new initial Event
        raw_title = analysis_data.get("event_title") or article.title
        categories = analysis_data.get("categories") or []
        primary_cat = categories[0] if categories else None

        new_event = Event(
            title=raw_title,
            event_type=primary_cat,
            countries=analysis_data.get("countries") or [],
            actors=analysis_data.get("actors") or [],
            location=analysis_data.get("location"),
            importance=analysis_data.get("importance", 3),
            status="active",
            parent_event_id=None,
            first_seen_at=ref_time,
            last_updated_at=ref_time,
        )
        session.add(new_event)
        await session.flush()

        return EventMatchResult(
            event_id=new_event.id,
            match_type="initial",
            confidence=1.0,
            is_new_event=True,
            event=new_event,
        )
