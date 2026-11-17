"""Editorial camps per source, and the grouping used by the narrative matrix.

Two outlets covering the same event do not describe it the same way, and a single
neutral summary hides exactly that difference. Phase T-2 therefore keeps one claim
per camp and shows the disagreement instead of flattening it.

The camp of a source lives in ``editorial/source_orientations.json`` rather than in
the ``sources`` table: the registry is editorial metadata that must stay readable and
diffable, and adding a column would need a migration for data no query filters on.
Sources without an entry are labelled ``other`` instead of being dropped, so a new
feed still appears in the matrix before someone classifies it.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterable
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from app.editorial.glossary import canonicalize_approved_names
from app.editorial.regions import (
    is_country,
    is_waterway,
    normalize_place,
)

logger = logging.getLogger(__name__)

ORIENTATIONS_PATH = (
    Path(__file__).resolve().parents[2] / "editorial" / "source_orientations.json"
)


@dataclass(frozen=True)
class Orientation:
    """One editorial camp: the label shown to the reader and the outlets in it."""

    key: str
    label_fa: str
    order: int
    sources: tuple[str, ...] = ()

    def matches(self, source_name: str) -> bool:
        wanted = _normalize(source_name)
        return bool(wanted) and any(_normalize(name) == wanted for name in self.sources)


UNKNOWN_ORIENTATION = Orientation(key="other", label_fa="سایر منابع", order=99)


def _normalize(name: str | None) -> str:
    return " ".join((name or "").split()).lower()


def build_orientations(payload: dict[str, Any] | None) -> tuple[Orientation, ...]:
    """Build the camp list from parsed JSON, ordered by display order."""
    orientations: list[Orientation] = []
    for raw in (payload or {}).get("orientations", []):
        key = str(raw.get("key") or "").strip()
        if not key:
            logger.warning("Ignoring orientation entry without a key: %r", raw)
            continue
        orientations.append(
            Orientation(
                key=key,
                label_fa=str(raw.get("label_fa") or key),
                order=int(raw.get("order", 50)),
                sources=tuple(
                    str(name).strip() for name in raw.get("sources", []) if str(name).strip()
                ),
            )
        )
    return tuple(sorted(orientations, key=lambda orientation: (orientation.order, orientation.key)))


@lru_cache(maxsize=1)
def load_orientations() -> tuple[Orientation, ...]:
    """Return the configured camps in display order (cached)."""
    try:
        payload = json.loads(ORIENTATIONS_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        logger.warning("%s is missing; every source is labelled 'other'.", ORIENTATIONS_PATH)
        payload = {"orientations": []}
    except json.JSONDecodeError as exc:
        logger.error("%s is not valid JSON (%s); every source is labelled 'other'.", ORIENTATIONS_PATH, exc)
        payload = {"orientations": []}
    return build_orientations(payload)


def reset_orientation_cache() -> None:
    """Drop the cached camps (used by tests and after editing the JSON file)."""
    load_orientations.cache_clear()


def orientation_for(source_name: str | None) -> Orientation:
    """Return the camp a source speaks for, or the neutral fallback."""
    for orientation in load_orientations():
        if orientation.matches(source_name or ""):
            return orientation
    return UNKNOWN_ORIENTATION


def orientation_keys() -> tuple[str, ...]:
    """Every configured camp key, in display order."""
    return tuple(orientation.key for orientation in load_orientations())


def distinct_orientations(source_names: Iterable[str]) -> set[str]:
    """The set of camps covering a group of sources, ignoring the neutral fallback."""
    keys = {orientation_for(name).key for name in source_names}
    keys.discard(UNKNOWN_ORIENTATION.key)
    return keys


def group_perspectives(
    perspectives: Iterable[dict[str, Any]],
) -> list[tuple[Orientation, list[dict[str, Any]]]]:
    """Group claim records by camp, in display order, dropping empty groups.

    A claim may carry an explicit ``orientation`` key from the analysis model; when it
    does, that key wins. Otherwise the camp is derived from the source name, which is
    what keeps a rerun of an older summary labelled consistently.
    """
    order = {orientation.key: orientation for orientation in load_orientations()}
    grouped: dict[str, list[dict[str, Any]]] = {}
    for record in perspectives or []:
        if not isinstance(record, dict):
            continue
        claim = str(record.get("claim_fa") or "").strip()
        if not claim:
            continue
        key = str(record.get("orientation") or "").strip()
        orientation = order.get(key) or orientation_for(record.get("source_name"))
        grouped.setdefault(orientation.key, []).append(record)

    result: list[tuple[Orientation, list[dict[str, Any]]]] = []
    for orientation in load_orientations():
        records = grouped.pop(orientation.key, None)
        if records:
            result.append((orientation, records))
    if grouped:
        leftover = [record for records in grouped.values() for record in records]
        result.append((UNKNOWN_ORIENTATION, leftover))
    return result


_COMPOUND_SPLIT_RE = re.compile(
    r"\s*(?:[,،;/|]|\s+[-–—]\s+|\s+(?:and|&|و)\s+)\s*",
    re.IGNORECASE,
)


def extract_non_country_entities(article_dict: dict[str, Any]) -> set[str]:
    """Extract non-country actors and specific non-country/non-waterway locations.

    Sovereign countries (regional & global) and strategic waterways/seas are removed.
    Compound locations (e.g. 'Tokyo, Japan', 'Japan and South Korea', 'توکیو، ژاپن',
    'قاهره / مصر', 'Beirut - Lebanon') and compound actors (e.g. 'Iran and Houthis',
    'ایران و انصارالله', 'Iran and Russia') are decomposed so that country segments
    are stripped while valid non-state actors and specific cities are retained.
    Hyphenated geographical names (e.g. 'Port-au-Prince', 'Tel-Aviv') are preserved intact.
    Approved glossary spellings are canonicalized so spelling variants (e.g. بوتین / پوتین)
    match as one entity.
    """
    raw_actors = article_dict.get("actors") or []
    location = article_dict.get("location") or ""
    entities: set[str] = set()

    for item in raw_actors:
        if not item:
            continue
        canon = canonicalize_approved_names(str(item))
        norm = normalize_place(canon)
        if not norm or len(norm) < 2:
            continue
        if is_country(norm) or is_waterway(norm):
            continue

        parts = [p.strip() for p in _COMPOUND_SPLIT_RE.split(canon) if p.strip()]
        if len(parts) > 1:
            for part in parts:
                p_norm = normalize_place(part)
                if not p_norm or len(p_norm) < 2:
                    continue
                if is_country(p_norm) or is_waterway(p_norm):
                    continue
                entities.add(p_norm)
        else:
            entities.add(norm)

    if location:
        canon_loc = canonicalize_approved_names(str(location))
        norm_loc = normalize_place(canon_loc)
        if norm_loc and len(norm_loc) >= 2:
            # If the entire location string is a country or waterway, discard it immediately
            if not (is_country(norm_loc) or is_waterway(norm_loc)):
                # Decompose compound locations (e.g. 'Tokyo, Japan', 'ژاپن و کره جنوبی', 'Port-au-Prince, Haiti')
                parts = [p.strip() for p in _COMPOUND_SPLIT_RE.split(canon_loc) if p.strip()]
                if len(parts) > 1:
                    for part in parts:
                        p_norm = normalize_place(part)
                        if not p_norm or len(p_norm) < 2:
                            continue
                        if is_country(p_norm) or is_waterway(p_norm):
                            continue
                        entities.add(p_norm)
                else:
                    entities.add(norm_loc)

    return entities


def check_perspectives_gate(
    camp_articles: list[dict[str, Any]],
    min_shared_actors: int = 1,
) -> tuple[bool, set[str]]:
    """Determine whether multi-camp coverage qualifies for perspective matrix generation.

    Gate Rule (Phase Y-13):
    Coverage qualifies ONLY if at least two different editorial camps share at least
    ``min_shared_actors`` non-country actor or city/specific location.
    If only broad countries (e.g. 'Yemen') or waterways match, the gate rejects to prevent
    spurious perspective comparisons between distinct, coincidental events.

    Recorded limitation:
    This gate cannot distinguish two completely separate incidents if they happen
    to involve the exact same specific actors in the same time frame.
    """
    if len(camp_articles) < 2:
        return False, set()

    camp_entities: list[set[str]] = [
        extract_non_country_entities(art) for art in camp_articles
    ]

    all_shared: set[str] = set()
    for i in range(len(camp_entities)):
        for j in range(i + 1, len(camp_entities)):
            shared = camp_entities[i] & camp_entities[j]
            if len(shared) >= min_shared_actors:
                all_shared |= shared

    return bool(all_shared), all_shared

