"""Temporary backfill helper for Phase 1+2 topic taxonomy. Delete after use."""
from __future__ import annotations

import asyncio
import json
import os
import sys
import traceback
from datetime import date
from pathlib import Path

print("HELLO_TOP_LEVEL", flush=True)

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

env_file = ROOT / ".env"
if env_file.is_file():
    from dotenv import load_dotenv

    load_dotenv(env_file)

print("IMPORTING_APP", flush=True)
from app.database.connection import SessionLocal  # noqa: E402
from app.editorial.telegraph_topics import group_items_by_telegraph_topic  # noqa: E402
from app.reports.telegraph_pagination import (  # noqa: E402
    LAYOUT_VERSION,
    PAGE_CONTENT_BUDGET_BYTES,
    layout_hash,
    measure_nodes,
    nav_reserve_bytes,
    plan_topic_parts,
)
from app.reports.telegraph_report import (  # noqa: E402
    build_item_block,
    build_page_overhead_bytes,
    fetch_complete_window_items,
    normalize_topic_entry,
    publish_telegraph_report,
    report_window,
)

print("IMPORTED_APP", flush=True)

_slot_date_env = os.environ.get("SLOT_DATE", "").strip()
_slot_hour_env = os.environ.get("SLOT_HOUR", "").strip()
if _slot_date_env and _slot_hour_env:
    _y, _m, _d = _slot_date_env.split("-")
    SLOTS = [(date(int(_y), int(_m), int(_d)), int(_slot_hour_env))]
else:
    SLOTS = [
        (date(2026, 11, 15), 8),
        (date(2026, 11, 16), 20),
        (date(2026, 11, 17), 8),
        (date(2026, 11, 17), 20),
        (date(2026, 11, 18), 8),
    ]

DRY_RUN = os.environ.get("DRY_RUN", "false").lower() in ("true", "1", "yes")
FORCE = os.environ.get("FORCE", "false").lower() in ("true", "1", "yes")
PERSIST = os.environ.get("PERSIST", "true").lower() in ("true", "1", "yes")
CHECK_ONLY = os.environ.get("CHECK_ONLY", "false").lower() in ("true", "1", "yes")
# PLAN_ONLY reads the production database and reports the layout it would publish. It never
# writes, never calls Telegraph and never touches Telegram or the slot status.
PLAN_ONLY = os.environ.get("PLAN_ONLY", "false").lower() in ("true", "1", "yes")


def _stored_state(raw: object) -> dict:
    entry = normalize_topic_entry(raw)
    active = [part for part in entry["parts"] if not part.get("retired")]
    return {
        "part_count": len(active),
        "layout_version": entry.get("layout_version"),
        "links_complete": entry.get("links_complete"),
        "planned_item_count": entry.get("planned_item_count"),
        "published_item_count": entry.get("published_item_count"),
        "layout_hash": entry.get("layout_hash"),
        "items_per_part": [part.get("item_count") for part in active],
    }


async def plan_only() -> None:
    """Report, per slot, the pages this code would publish - without publishing them."""
    from sqlalchemy import select

    from app.database.models import TelegraphReport

    report_rows = []
    for slot_date, slot_hour in SLOTS:
        _d, window_start, window_end = report_window(slot_date, slot_hour)
        async with SessionLocal() as session:
            # Same fetch the publisher uses: the whole window, nothing trimmed.
            _d, items, metadata = await fetch_complete_window_items(
                session, report_date=slot_date, slot_hour=slot_hour
            )
            stmt = select(TelegraphReport).where(
                TelegraphReport.report_date == slot_date,
                TelegraphReport.slot_hour == slot_hour,
            )
            row = (await session.execute(stmt)).scalar_one_or_none()

        stored_pages = dict(getattr(row, "topic_pages", None) or {}) if row else {}
        overhead = build_page_overhead_bytes(slot_date, slot_hour, window_start, window_end)
        sections = group_items_by_telegraph_topic(items)
        unique_ids = {
            item.get("event_id") if item.get("event_id") is not None else id(item)
            for _key, _name, topic_items in sections
            for item in topic_items
        }

        topics = []
        failures = []
        planned_parts_total = 0
        for key, name, topic_items in sections:
            try:
                parts = plan_topic_parts(
                    topic_items,
                    block_builder=build_item_block,
                    overhead_bytes=overhead,
                    topic=name,
                )
            except Exception as exc:  # report the slot instead of aborting the whole run
                failures.append({"topic": key, "error": str(exc)})
                continue
            reserve = nav_reserve_bytes(len(parts))
            block_bytes = [
                measure_nodes(build_block_chain(part.items)) for part in parts
            ]
            planned_parts_total += len(parts)
            stored = _stored_state(stored_pages.get(key))
            current = (
                stored["layout_version"] == LAYOUT_VERSION
                and stored["links_complete"] is True
                and stored["layout_hash"] == layout_hash(parts)
                and stored["planned_item_count"] == len(topic_items)
                and stored["published_item_count"] == len(topic_items)
            )
            topics.append(
                {
                    "key": key,
                    "name_fa": name,
                    "items": len(topic_items),
                    "parts": [part.item_count for part in parts],
                    "upper_part_bytes": [overhead + reserve + size for size in block_bytes],
                    "stored": stored,
                    "needs_rebuild": not current,
                }
            )

        report_rows.append(
            {
                "slot": f"{slot_date.isoformat()} {slot_hour:02d}:00",
                "window": [window_start.isoformat(), window_end.isoformat()],
                "item_count": sum(len(topic_items) for _k, _n, topic_items in sections),
                "unique_item_count": len(unique_ids),
                "metadata": {
                    key: metadata.get(key)
                    for key in (
                        "articles_without_summary",
                        "unlinked_articles",
                        "flagged_events_included",
                    )
                    if key in metadata
                },
                "planned_parts": planned_parts_total,
                "planned_pages_with_index": planned_parts_total + 1,
                "overhead_bytes": overhead,
                "page_budget_bytes": PAGE_CONTENT_BUDGET_BYTES,
                "topics": topics,
                "topics_needing_rebuild": [t["key"] for t in topics if t["needs_rebuild"]],
                "stored_status": getattr(row, "status", None),
                "stored_index_url": getattr(row, "index_url", None),
                "stored_topic_keys": sorted(stored_pages),
                "unplanned_stored_topics": sorted(set(stored_pages) - {t["key"] for t in topics}),
                "failures": failures,
            }
        )
        print("PLAN_SLOT " + json.dumps(report_rows[-1], ensure_ascii=False), flush=True)

    Path("plan_only_report.json").write_text(
        json.dumps(report_rows, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print("PLAN_ONLY_DONE", flush=True)


def build_block_chain(items):
    nodes: list = []
    for index, item in enumerate(items):
        if index:
            nodes.append({"tag": "hr"})
        nodes.extend(build_item_block(item))
    return nodes


async def main() -> None:
    if PLAN_ONLY:
        print("PLAN_ONLY_MODE", flush=True)
        await plan_only()
        return
    if CHECK_ONLY:
        print("CHECK_ONLY_MODE", flush=True)
        from sqlalchemy import select

        from app.database.models import TelegraphReport
        for rd, sh in SLOTS:
            async with SessionLocal() as session:
                stmt = select(TelegraphReport).where(TelegraphReport.report_date == rd, TelegraphReport.slot_hour == sh)
                row = (await session.execute(stmt)).scalar_one_or_none()
                if row is None:
                    print(json.dumps({"slot": f"{rd} {sh:02d}:00", "row": None}, ensure_ascii=False), flush=True)
                else:
                    tp = row.topic_pages or {}
                    print(json.dumps({
                        "slot": f"{rd.isoformat()} {sh:02d}:00",
                        "status": row.status,
                        "telegram_status": getattr(row, "telegram_status", None),
                        "index_url": row.index_url,
                        "index_path": row.index_path,
                        "updated_at": str(row.updated_at),
                        "error_message": row.error_message,
                        "topic_count": len(tp),
                        "topics": {
                            k: {"url": (v or {}).get("url", ""), "n": (v or {}).get("item_count")}
                            for k, v in tp.items()
                        },
                    }, ensure_ascii=False), flush=True)
        print("CHECK_DONE", flush=True)
        return
    print("MAIN_STARTED", flush=True)
    print("SLOTS_LEN=" + str(len(SLOTS)), flush=True)
    results = []
    for rd, sh in SLOTS:
        print("LOOP_ITER=" + str(rd) + " " + str(sh), flush=True)
        try:
            async with SessionLocal() as session:
                print("SESSION_OPENED", flush=True)
                result = await publish_telegraph_report(
                    session,
                    report_date=rd,
                    slot_hour=sh,
                    dry_run=DRY_RUN,
                    force=FORCE,
                    persist=PERSIST,
                )
                print("RESULT_OK", flush=True)
                rec = {
                    "slot": f"{rd.isoformat()} {sh:02d}:00",
                    "status": result.get("status"),
                    "item_count": result.get("item_count", 0),
                    "created": result.get("created", 0),
                    "updated": result.get("updated", 0),
                    "reused": result.get("reused", 0),
                    "index_url": result.get("index_url", ""),
                    "pages": {
                        k: {"url": (v or {}).get("url", ""), "n": (v or {}).get("item_count")}
                        for k, v in (result.get("pages") or {}).items()
                    },
                    "failures": result.get("failures", []),
                }
                print("SLOT_DONE " + json.dumps(rec, ensure_ascii=False), flush=True)
                results.append(rec)
        except Exception as exc:
            print("EXCEPTION_CAUGHT", flush=True)
            print(repr(exc), flush=True)
            traceback.print_exc()
            raise
    print("ALL_DONE", flush=True)
    Path("backfill_results.json").write_text(
        json.dumps({"dry_run": DRY_RUN, "force": FORCE, "persist": PERSIST, "results": results}, ensure_ascii=False),
        encoding="utf-8",
    )
    print("WROTE_FILE", flush=True)


print("CALLING_MAIN", flush=True)
asyncio.run(main())
print("SCRIPT_FINISHED", flush=True)
