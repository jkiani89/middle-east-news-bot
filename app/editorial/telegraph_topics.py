"""Section titles and ordering for the Telegraph report index.

The editorial topic rule (``app.editorial.topics``) decides which topic an event belongs
to. This module only decides how those topics are titled and ordered on the index page.
Grouping is total: an item whose topic is unknown (or missing) lands in ``other``, so a
presentation lookup can never silently remove a news item from the report.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

TELEGRAPH_TOPICS_PATH = Path(__file__).resolve().parents[2] / "editorial" / "telegraph_topics.json"

OTHER_TOPIC_KEY = "other"
OTHER_TOPIC_NAME = "سایر تحولات"

# A news item that was collected but never analysed (still ``collected``, or parked as
# ``expired`` before any AI call) is published under its own heading instead of
# "سایر تحولات": the generic section must mean "analysed, but no specific subject",
# not "no analysis at all".
UNPROCESSED_TOPIC_KEY = "unprocessed"
UNPROCESSED_TOPIC_NAME = "اخبار پردازش‌نشده"

# Items the pipeline did process but did not publish (editorially rejected, or failed).
# Keeping them here means they stay in the report without being counted as "unprocessed".
UNPUBLISHED_TOPIC_KEY = "not_published"
UNPUBLISHED_TOPIC_NAME = "اخبار بررسی‌شده بدون انتشار"


@dataclass(frozen=True)
class TelegraphTopic:
    """One index section."""

    key: str
    name_fa: str
    order: int
    icon: str = ""


DEFAULT_TOPICS: tuple[TelegraphTopic, ...] = (
    TelegraphTopic(key=OTHER_TOPIC_KEY, name_fa=OTHER_TOPIC_NAME, order=99, icon="📌"),
    TelegraphTopic(key=UNPROCESSED_TOPIC_KEY, name_fa=UNPROCESSED_TOPIC_NAME, order=97, icon="🗂"),
    TelegraphTopic(key=UNPUBLISHED_TOPIC_KEY, name_fa=UNPUBLISHED_TOPIC_NAME, order=98, icon="📋"),
)


def build_telegraph_topics(payload: dict[str, Any] | None) -> tuple[TelegraphTopic, ...]:
    topics: list[TelegraphTopic] = []
    seen: set[str] = set()
    for raw in (payload or {}).get("topics", []):
        key = str(raw.get("key") or "").strip()
        if not key or key in seen:
            continue
        seen.add(key)
        topics.append(
            TelegraphTopic(
                key=key,
                name_fa=str(raw.get("name_fa") or key),
                order=int(raw.get("order", 50)),
                icon=str(raw.get("icon") or ""),
            )
        )
    if OTHER_TOPIC_KEY not in seen:
        topics.append(TelegraphTopic(OTHER_TOPIC_KEY, OTHER_TOPIC_NAME, 99, icon="📌"))
    # Presentation-only sections: always available so an item that carries no analysis
    # never falls back into "سایر تحولات", and never disappears when it is empty.
    if UNPROCESSED_TOPIC_KEY not in seen:
        topics.append(TelegraphTopic(UNPROCESSED_TOPIC_KEY, UNPROCESSED_TOPIC_NAME, 97, icon="🗂"))
    if UNPUBLISHED_TOPIC_KEY not in seen:
        topics.append(TelegraphTopic(UNPUBLISHED_TOPIC_KEY, UNPUBLISHED_TOPIC_NAME, 98, icon="📋"))
    return tuple(sorted(topics, key=lambda topic: (topic.order, topic.key)))


@lru_cache(maxsize=1)
def load_telegraph_topics() -> tuple[TelegraphTopic, ...]:
    try:
        payload = json.loads(TELEGRAPH_TOPICS_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        logger.warning("%s is missing; the index uses the generic section only.", TELEGRAPH_TOPICS_PATH)
        return DEFAULT_TOPICS
    except json.JSONDecodeError as exc:
        logger.error("%s is invalid JSON (%s); the index uses the generic section only.", TELEGRAPH_TOPICS_PATH, exc)
        return DEFAULT_TOPICS
    return build_telegraph_topics(payload)


def reset_telegraph_topic_cache() -> None:
    load_telegraph_topics.cache_clear()


def telegraph_topic(key: str | None) -> TelegraphTopic:
    """Resolve a topic key, falling back to the generic section."""
    topics = load_telegraph_topics()
    for topic in topics:
        if key and topic.key == key:
            return topic
    for topic in topics:
        if topic.key == OTHER_TOPIC_KEY:
            return topic
    return TelegraphTopic(OTHER_TOPIC_KEY, OTHER_TOPIC_NAME, 99)


def _item_topic_keys(item: dict[str, Any]) -> list[Any]:
    """Resolve the list of topic keys an item belongs to (multi-topic aware).

    An empty ``topic_keys`` list falls back to ``topic_key`` so a stale item keeps
    its original subject instead of being silently moved to "other".
    """
    keys = item.get("topic_keys")
    if isinstance(keys, (list, tuple)):
        if keys:
            return list(keys)
        return [item.get("topic_key")]
    if keys is not None:
        return [keys]
    return [item.get("topic_key")]


def _item_identity(item: dict[str, Any]) -> Any:
    """Stable identity for dedup within one section: prefer ``event_id``."""
    event_id = item.get("event_id")
    if event_id is not None:
        return ("event", event_id)
    return ("id", id(item))


def group_items_by_telegraph_topic(
    items: list[dict[str, Any]],
) -> list[tuple[str, str, list[dict[str, Any]]]]:
    """Group items into index sections, preserving the configured order and every item.

    An item may belong to several topics (multi-topic): it is then published in each
    of those sections. No item is dropped, and duplicates inside one section are
    collapsed by a stable identity (``event_id`` when available, else object id).
    """
    grouped: dict[str, list[dict[str, Any]]] = {}
    seen_in_section: dict[str, set[Any]] = {}
    for item in items:
        for key in _item_topic_keys(item):
            resolved = telegraph_topic(key).key
            bucket = grouped.setdefault(resolved, [])
            seen = seen_in_section.setdefault(resolved, set())
            identity = _item_identity(item)
            if identity in seen:
                continue
            seen.add(identity)
            bucket.append(item)

    sections: list[tuple[str, str, list[dict[str, Any]]]] = []
    for topic in load_telegraph_topics():
        topic_items = grouped.pop(topic.key, None)
        if topic_items:
            sections.append((topic.key, topic.name_fa, topic_items))
    # A topic present in the data but absent from the JSON must still be published.
    for key, topic_items in grouped.items():
        sections.append((key, telegraph_topic(key).name_fa, topic_items))
    return sections
