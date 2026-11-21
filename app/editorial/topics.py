"""Briefing topics and the primary-subject rule that assigns a story to one.

The channel used to be Middle-East-only: ``regions.is_primary_actor`` rejected any
story about Ukraine or US domestic politics, so two of the requested briefings
could never contain anything. A story now qualifies when it is the *primary
subject* of a configured topic from ``editorial/topics.json``; the Middle East
topic delegates to the audited regional rule so that behaviour is unchanged.

The rule stays strict on purpose: a country appearing in a long list is not
enough, one of the topic's actors or places must corroborate it.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from app.editorial.newsworthiness import non_political_story_reason
from app.editorial.regions import (
    MIDDLE_EAST_CITIES,
    MIDDLE_EAST_COUNTRIES,
    _name_appears,
    is_primary_actor,
    normalize_place,
)

logger = logging.getLogger(__name__)

TOPICS_PATH = Path(__file__).resolve().parents[2] / "editorial" / "topics.json"

REGIONAL_TOPIC_KEY = "me"


@dataclass(frozen=True)
class Topic:
    """One briefing section and the rule that decides whether a story belongs to it."""

    key: str
    name_fa: str
    priority: int
    countries: frozenset[str] = frozenset()
    actors: tuple[str, ...] = ()
    places: tuple[str, ...] = ()
    keywords: tuple[str, ...] = ()
    min_importance: int = 2
    # Countries that must be named in the analysis for the topic to apply at all.
    # ``us-cn`` needs this: its ``countries`` list includes the United States for the
    # actor match, but a story about America alone is not a China story.
    anchor_countries: frozenset[str] = frozenset()

    def matches_primary(
        self,
        countries: list[str] | None,
        actors: list[str] | None,
        location: str | None = None,
        title: str | None = None,
        importance: int | None = None,
    ) -> bool:
        """Whether the story is *about* this topic rather than a passing mention."""
        if importance is not None and importance < self.min_importance:
            return False
        if self.key == REGIONAL_TOPIC_KEY:
            # The regional topic keeps the audited Middle East rule verbatim.
            return is_primary_actor(countries, actors, location)

        normalized_countries = {normalize_place(c) for c in countries or [] if c}
        if self.anchor_countries and not any(
            _name_appears(anchor, name)
            for anchor in self.anchor_countries
            for name in normalized_countries
        ):
            return False
        actor_text = normalize_place(" | ".join(actors or []))
        location_text = normalize_place(location or "")
        title_text = normalize_place(title or "")

        country_hit = any(
            _name_appears(country, name)
            for country in self.countries
            for name in normalized_countries
        )
        actor_hit = any(_name_appears(alias, actor_text) for alias in self.actors) if actor_text else False
        place_hit = any(_name_appears(place, location_text) for place in self.places) if location_text else False
        keyword_hit = any(_name_appears(word, title_text) for word in self.keywords) if title_text else False
        # A country name appearing in the title is not enough corroboration: it
        # would make every story about that country a topic. Only substantive
        # (non-country) keywords count when the AI omitted the regional actor.
        country_words = {normalize_place(c) for c in (*self.countries, *self.anchor_countries)}
        substantive_keyword_hit = any(
            _name_appears(word, title_text)
            and normalize_place(word) not in country_words
            for word in self.keywords
        ) if title_text else False

        if country_hit:
            # A substantive keyword in the title can corroborate the topic even
            # when the AI omitted the regional actor (e.g. "US Treasury sanctions
            # Iranian oil revenues"), but a bare country name cannot.
            return actor_hit or place_hit or (keyword_hit and substantive_keyword_hit)
        # No configured country named in the analysis: the title and a place or actor
        # must both point at the topic (e.g. "Kyiv drone strike" with countries=[]).
        return keyword_hit and (actor_hit or place_hit)


REGIONAL_TOPIC = Topic(
    key=REGIONAL_TOPIC_KEY,
    name_fa="تحولات خاورمیانه",
    priority=99,
    countries=frozenset(MIDDLE_EAST_COUNTRIES),
    places=tuple(sorted(MIDDLE_EAST_CITIES)),
)


def build_topics(payload: dict[str, Any] | None) -> tuple[Topic, ...]:
    """Build the topic list from parsed JSON, always including the regional topic."""
    topics: list[Topic] = []
    for raw in (payload or {}).get("topics", []):
        key = str(raw.get("key") or "").strip()
        if not key or key == REGIONAL_TOPIC_KEY:
            logger.warning("Ignoring topic entry with missing or reserved key: %r", raw)
            continue
        topics.append(
            Topic(
                key=key,
                name_fa=str(raw.get("name_fa") or key),
                priority=int(raw.get("priority", 50)),
                countries=frozenset(
                    normalize_place(c) for c in raw.get("countries", []) if str(c).strip()
                ),
                actors=tuple(
                    normalize_place(a) for a in raw.get("actors", []) if str(a).strip()
                ),
                places=tuple(
                    normalize_place(p) for p in raw.get("places", []) if str(p).strip()
                ),
                keywords=tuple(
                    normalize_place(k) for k in raw.get("keywords", []) if str(k).strip()
                ),
                min_importance=int(raw.get("min_importance", 2)),
                anchor_countries=frozenset(
                    normalize_place(c) for c in raw.get("anchor_countries", []) if str(c).strip()
                ),
            )
        )
    topics.append(REGIONAL_TOPIC)
    return tuple(sorted(topics, key=lambda topic: topic.priority))


@lru_cache(maxsize=1)
def load_topics() -> tuple[Topic, ...]:
    """Return the configured topics in priority order (cached)."""
    try:
        payload = json.loads(TOPICS_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        logger.warning("%s is missing; only the Middle East topic is active.", TOPICS_PATH)
        payload = {"topics": []}
    except json.JSONDecodeError as exc:
        logger.error("%s is not valid JSON (%s); only the Middle East topic is active.", TOPICS_PATH, exc)
        payload = {"topics": []}
    return build_topics(payload)


def reset_topic_cache() -> None:
    """Drop the cached topics (used by tests and after editing the JSON file)."""
    load_topics.cache_clear()


def topic_by_key(key: str) -> Topic | None:
    for topic in load_topics():
        if topic.key == key:
            return topic
    return None


def match_topics(
    countries: list[str] | None,
    actors: list[str] | None,
    location: str | None = None,
    title: str | None = None,
    importance: int | None = None,
    summary: str | None = None,
) -> list[Topic]:
    """Return every topic the story is primarily about (multi-topic).

    Specific topics are evaluated first; the regional Middle East topic is used only
    as a fallback when no specific topic matches, so a story is never duplicated
    into the generic regional section on top of a more specific one. The Iran topic
    covers the full diplomatic, economic, security and military domain, not only
    negotiations.

    A regional story that is only sport, tourism or a routine local incident gets no
    topic at all: it cleared the geography gate but carries nothing to brief.
    """
    if non_political_story_reason(title, summary, countries, actors, location):
        return []

    result: list[Topic] = []
    for topic in load_topics():
        if topic.key == REGIONAL_TOPIC_KEY:
            continue
        if topic.matches_primary(countries, actors, location, title, importance):
            result.append(topic)
    if not result:
        regional = topic_by_key(REGIONAL_TOPIC_KEY)
        if regional is not None and regional.matches_primary(
            countries, actors, location, title, importance
        ):
            result.append(regional)
    return result


def match_primary_topic(
    countries: list[str] | None,
    actors: list[str] | None,
    location: str | None = None,
    title: str | None = None,
    importance: int | None = None,
) -> Topic | None:
    """Return the most specific topic the story is primarily about, if any.

    Backward-compatible single-topic entry point; delegates to :func:`match_topics`.
    """
    topics = match_topics(countries, actors, location, title, importance)
    return topics[0] if topics else None


def topics_for_event(event: Any) -> list[Topic]:
    """Classify a stored ``Event`` row into every matching topic."""
    return match_topics(
        getattr(event, "countries", None),
        getattr(event, "actors", None),
        getattr(event, "location", None),
        getattr(event, "title", None),
        getattr(event, "importance", None),
        getattr(event, "summary_fa", None),
    )


def topic_for_event(event: Any) -> Topic | None:
    """Classify a stored ``Event`` row using its analysis fields (primary topic)."""
    topics = topics_for_event(event)
    return topics[0] if topics else None
