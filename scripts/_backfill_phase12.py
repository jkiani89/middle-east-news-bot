"""Temporary backfill helper for Phase 1+2 topic taxonomy.

Runs publish_telegraph_report directly for five explicit slots. Does NOT perform
collection, scheduling, or Telegram sending. Delete after use.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

env_file = ROOT / ".env"
if env_file.is_file():
    from dotenv import load_dotenv

    load_dotenv(env_file)

from app.config import settings  # noqa: E402
from app.database.connection import SessionLocal  # noqa: E402
from app.reports.telegraph_report import publish_telegraph_report  # noqa: E402

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
    for rd, sh in SLOTS:
        async with SessionLocal() as session:
            result = await publish_telegraph_report(
                session,
                report_date=rd,
                slot_hour=sh,
                dry_run=DRY_RUN,
                force=FORCE,
                persist=PERSIST,
            )
            print(
                json.dumps(
                    {
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
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
    print(
        json.dumps(
            {
                "summary": True,
                "dry_run": DRY_RUN,
                "force": FORCE,
                "persist": PERSIST,
                "slots": len(SLOTS),
            },
            ensure_ascii=False,
        ),
        flush=True,
    )


if __name__ == "__main__":
    asyncio.run(main())
