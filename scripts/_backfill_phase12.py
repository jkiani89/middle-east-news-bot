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
                    print(json.dumps({"slot": f"{rd} {sh:02d}:00", "status": row.status, "index_url": row.index_url, "topics": list((row.topic_pages or {}).keys()), "item_counts": {k: (v or {}).get("item_count") for k, v in (row.topic_pages or {}).items()}}, ensure_ascii=False), flush=True)
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
                rec = {"slot": f"{rd.isoformat()} {sh:02d}:00", "status": result.get("status"), "item_count": result.get("item_count", 0)}
                print("SLOT_DONE " + str(rec), flush=True)
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
