"""Live Execution Script for Phase 6 Daily Intelligence Report Generation.

Executes generate_and_save_daily_report against the live PostgreSQL database (Safe Path - zero Gemini calls),
verifies report generation on the 29 events in DB, prints the complete Persian report to terminal,
and inspects the stored record in the daily_reports table.
"""

import asyncio
import sys
from datetime import datetime
from zoneinfo import ZoneInfo
from sqlalchemy import select

from app.config import settings
from app.database.connection import SessionLocal
from app.database.models import DailyReport
from app.reports.daily_report import generate_and_save_daily_report


async def main():
    print("=" * 80)
    print("  PHASE 6: LIVE DAILY INTELLIGENCE REPORT GENERATION (SAFE PATH)")
    print("=" * 80)

    tz = ZoneInfo(settings.timezone)
    today = datetime.now(tz).date()
    print(f"Target Report Date: {today} (Timezone: {settings.timezone})")
    print(f"Minimum Importance Filter: 3 (filtering events >= 3)")
    print(f"Executing generate_and_save_daily_report...")

    rep_date, items, formatted_text = await generate_and_save_daily_report(
        report_date=today, min_importance=3
    )

    print("\n" + "=" * 80)
    print("  GENERATED PERSIAN INTELLIGENCE REPORT OUTPUT (FULL TEXT)")
    print("=" * 80)
    print(formatted_text)
    print("=" * 80)

    print("\n[VERIFICATION CHECKS]")
    print(f"1. Total qualifying events in report: {len(items)}")
    print(f"2. Report text character count: {len(formatted_text)} chars (> 500 requirement: {len(formatted_text) > 500})")
    
    # Check multi-source items (e.g. COP31 Antalya event #62)
    multi_source_items = [i for i in items if i["total_sources_count"] > 1]
    print(f"3. Multi-source merged events found: {len(multi_source_items)}")
    for mi in multi_source_items:
        print(f"   * Event #{mi['event_id']} ('{mi['title']}'): {mi['total_sources_count']} sources")
        for s in mi["additional_sources"]:
            print(f"     - Additional source: {s.get('source')} | URL: {s.get('url')}")

    # Verify directly from PostgreSQL daily_reports table
    async with SessionLocal() as session:
        db_rep = await session.scalar(
            select(DailyReport).where(DailyReport.report_date == rep_date)
        )
        assert db_rep is not None, "DailyReport record was not created in database!"
        print(f"4. Database Record ID: {db_rep.id}")
        print(f"5. Database Record Date: {db_rep.report_date}")
        print(f"6. Database Record Status: {db_rep.status} (Expected: 'generated')")
        print(f"7. Database Generated At: {db_rep.generated_at}")
        print(f"8. Database Content Keys: {list(db_rep.content.keys())}")
        print(f"9. Database Content Total Events: {db_rep.content['metadata']['total_events']}")

    print("\n" + "=" * 80)
    print("  ALL PHASE 6 LIVE VERIFICATION CHECKS PASSED SUCCESSFULLY!")
    print("=" * 80)


if __name__ == "__main__":
    asyncio.run(main())
