"""Deterministic splitting of one Telegraph topic into several pages.

Telegraph rejects a page whose serialised content exceeds 64 KB, and a topic can hold
hundreds of news blocks. The old code trimmed the tail of an oversized page, which
silently deleted news; this module plans the split instead.

Design rules:

* The unit is a **whole news block** (:func:`app.reports.telegraph_report.build_item_block`).
  A block is never divided, so the title, summary, source, time, verification label and
  original link of an item always live on the same page.
* Parts are **contiguous slices in the original order**, so concatenating them reproduces
  the section list exactly.
* Planning is **pure and deterministic**: the same items always produce the same bytes,
  the same part boundaries and the same ``layout_hash``. Idempotency depends on it.
* No part is ever empty, and no part may exceed the byte budget.

The budget reserves room for the navigation block (link chain) before the content is
packed. The number of parts and the size of that reserve depend on each other, so the plan
is computed as a fixed point: pack, see how many parts came out, reserve for that many,
pack again.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from app.reports.telegraph_client import MAX_CONTENT_BYTES, TelegraphContentTooLargeError

# Telegraph's documented ceiling is 64 KB of serialised content. Publishing at 60 KB leaves
# room for the differences between our measurement and the service's own validation.
PAGE_CONTENT_BUDGET_BYTES = 60 * 1024
# Fixed part of the navigation block: the intro sentence, the footer links line and the
# single compact identity line (``شناسه: 20261117T2000|us-cn|40|sha1:<40 hex>``, ~110
# bytes) that lets a later run prove a page's identity and content fingerprint. It
# replaces the two older markers (layout ~96 + identity ~98 bytes).
NAV_BASE_BYTES = 500
# Worst-case cost of one part link (label + a long percent-encoded Persian path).
NAV_LINK_RESERVE_BYTES = 340
# A topic that needs more parts than this has an abnormal amount of data: fail loudly.
MAX_PARTS_PER_TOPIC = 40
LAYOUT_VERSION = 2
# Separator node between two blocks, measured as if it were a one-line paragraph.
HR_NODE_BYTES = 24


@dataclass(frozen=True)
class TopicPart:
    """One page of a topic: the items it carries, in order."""

    part: int
    items: tuple[dict[str, Any], ...]
    used_bytes: int

    @property
    def item_count(self) -> int:
        return len(self.items)


def measure_nodes(nodes: list[dict[str, Any]]) -> int:
    """Byte size of node content exactly as it is sent to Telegraph."""
    return len(json.dumps([node for node in nodes if node], ensure_ascii=False).encode("utf-8"))


def nav_reserve_bytes(part_count: int) -> int:
    """Bytes to keep free for the navigation chain of a topic with ``part_count`` parts."""
    return NAV_BASE_BYTES + 2 * part_count * NAV_LINK_RESERVE_BYTES


def part_layout_hash(items: tuple[dict[str, Any], ...] | list[dict[str, Any]]) -> str:
    """Fingerprint of one part's content: which news items, in order."""
    payload = {"items": [item_identity(item) for item in items]}
    digest = hashlib.sha1(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    )
    return "sha1:" + digest.hexdigest()


def item_identity(item: dict[str, Any]) -> str:
    """Stable identity of a news item, used for the layout hash and duplicate detection."""
    event_id = item.get("event_id")
    if event_id is not None:
        return f"event:{int(event_id)}"
    article_id = item.get("article_id")
    if article_id is not None:
        return f"article:{int(article_id)}"
    payload = f"{item.get('headline_fa') or ''}|{item.get('primary_url') or ''}"
    return "digest:" + hashlib.sha1(payload.encode("utf-8")).hexdigest()


def layout_hash(parts: list[TopicPart]) -> str:
    """Fingerprint of a layout: which items, in which order, on which page."""
    payload = {
        "layout_version": LAYOUT_VERSION,
        "parts": [[item_identity(item) for item in part.items] for part in parts],
    }
    digest = hashlib.sha1(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8"))
    return "sha1:" + digest.hexdigest()


def _pack(
    items: list[dict[str, Any]],
    *,
    block_builder: Callable[[dict[str, Any]], list[dict[str, Any]]],
    usable_bytes: int,
    topic: str,
) -> list[list[dict[str, Any]]]:
    """Greedy contiguous packing of whole blocks into one page's worth of bytes."""
    parts: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    current_bytes = 0
    for item in items:
        block_bytes = measure_nodes(block_builder(item)) + HR_NODE_BYTES
        if block_bytes > usable_bytes:
            raise TelegraphContentTooLargeError(
                block_bytes + (PAGE_CONTENT_BUDGET_BYTES - usable_bytes),
                PAGE_CONTENT_BUDGET_BYTES,
                detail=(
                    f"a single news block of topic {topic!r} "
                    f"({item_identity(item)}) does not fit on one page even alone"
                ),
            )
        if current and current_bytes + block_bytes > usable_bytes:
            parts.append(current)
            current, current_bytes = [], 0
        current.append(item)
        current_bytes += block_bytes
    if current:
        parts.append(current)
    return parts


def plan_topic_parts(
    items: list[dict[str, Any]],
    *,
    block_builder: Callable[[dict[str, Any]], list[dict[str, Any]]],
    overhead_bytes: int,
    topic: str = "",
    budget_bytes: int = PAGE_CONTENT_BUDGET_BYTES,
    max_parts: int = MAX_PARTS_PER_TOPIC,
) -> list[TopicPart]:
    """Split ``items`` into as many pages as the byte budget requires, in order.

    ``overhead_bytes`` is the measured size of everything on the page that is not a news
    block (header, footer, separators). Navigation is reserved separately because its size
    depends on the number of parts, which is what this function is deciding.
    """
    if not items:
        return []

    guess = 1
    for _ in range(max_parts + 2):
        reserve = nav_reserve_bytes(guess)
        usable = budget_bytes - overhead_bytes - reserve
        if usable <= 0:
            raise TelegraphContentTooLargeError(
                overhead_bytes + reserve,
                budget_bytes,
                detail=f"the page chrome of topic {topic!r} leaves no room for news",
            )
        packed = _pack(items, block_builder=block_builder, usable_bytes=usable, topic=topic)
        if len(packed) > max_parts:
            raise TelegraphContentTooLargeError(
                len(packed) * budget_bytes,
                budget_bytes,
                detail=f"topic {topic!r} needs {len(packed)} parts (limit {max_parts})",
            )
        if len(packed) <= guess:
            return [
                TopicPart(part=index + 1, items=tuple(chunk), used_bytes=overhead_bytes + reserve)
                for index, chunk in enumerate(packed)
            ]
        guess = len(packed)

    raise TelegraphContentTooLargeError(
        len(items) * budget_bytes,
        budget_bytes,
        detail=f"the page layout of topic {topic!r} did not converge",
    )


def fits_in_one_page(nodes: list[dict[str, Any]]) -> bool:
    """Whether already-built content fits Telegraph's hard limit."""
    return measure_nodes(nodes) <= MAX_CONTENT_BYTES


def page_budget_ok(nodes: list[dict[str, Any]]) -> bool:
    """Whether already-built content respects the publishing budget (not the hard limit)."""
    return measure_nodes(nodes) <= PAGE_CONTENT_BUDGET_BYTES
