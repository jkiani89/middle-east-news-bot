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


async def main() -> None:
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
                    "index_url": result.get("index_url", ""),
                    "item_count": result.get("item_count", 0),
                    "created": result.get("created", 0),
                    "updated": result.get("updated", 0),
                    "reused": result.get("reused", 0),
                    "failures": result.get("failures", []),
                    "sections": result.get("sections", []),
                    "pages": {
                        k: (v or {}).get("item_count")
                        for k, v in (result.get("pages") or {}).items()
                    },
                }
                print(json.dumps(rec, ensure_ascii=False), flush=True)
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
