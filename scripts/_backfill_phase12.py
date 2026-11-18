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
from app.config import settings  # noqa: E402
from app.database.connection import SessionLocal  # noqa: E402
from app.reports.telegraph_report import publish_telegraph_report  # noqa: E402
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


async def main() -> None:
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
