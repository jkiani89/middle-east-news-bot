"""Cheap, log-only relevance prefilter over the text the model would see (Phase Y-11).

Roughly 43% of the analysed articles came back ``rejected`` from the model, each one
after a paid Gemini call. This module only *records* what a free lexical check would
have said, in ``ai_payload['prefilter']``; it never rejects an article, changes a
status or adds a model call. ``docs/sql/prefilter_stats.sql`` compares its verdict
with the model's own ``is_relevant`` to expose false negatives before anyone wires it
into the pipeline.
"""

from __future__ import annotations

from app.editorial.regions import (
    MIDDLE_EAST_CITIES,
    MIDDLE_EAST_COUNTRIES,
    MIDDLE_EAST_WATERWAYS,
    _name_appears,
    normalize_place,
)
from app.editorial.topics import load_topics

REASON_REGIONAL_MENTION = "regional_mention"
REASON_NO_REGIONAL_MENTION = "no_regional_mention"

# The regional vocabulary plus the configured topics' own countries, actors and places.
# Topics are loaded once per call (they are cached) so an edited topics.json is honoured.
_REGION_TERMS: frozenset[str] = frozenset(
    (*MIDDLE_EAST_COUNTRIES, *MIDDLE_EAST_CITIES, *MIDDLE_EAST_WATERWAYS)
)


def _terms() -> tuple[str, ...]:
    configured: set[str] = set()
    for topic in load_topics():
        for term in (*topic.countries, *topic.actors, *topic.places):
            if term:
                configured.add(term)
    return tuple(sorted(_REGION_TERMS | configured))


def prefilter_analysis(title: str, body: str) -> dict[str, object]:
    """Whether a cheap lexical check would reject the story, and why.

    Only names are matched (countries, cities, waterways, topic actors/places): a
    title-only keyword match would be too weak to be a useful signal. This function
    is deliberately side-effect free.
    """
    haystack = normalize_place(f"{title or ''}\n{body or ''}")
    if not haystack:
        return {"would_reject": True, "reason": REASON_NO_REGIONAL_MENTION}
    for term in _terms():
        if _name_appears(term, haystack):
            return {"would_reject": False, "reason": REASON_REGIONAL_MENTION}
    return {"would_reject": True, "reason": REASON_NO_REGIONAL_MENTION}
