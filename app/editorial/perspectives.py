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
from collections.abc import Iterable
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

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
