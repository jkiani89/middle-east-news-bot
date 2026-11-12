"""Single-run batch pipeline execution for GitHub Actions or scheduled runner.

Performs:
1. Source synchronization from editorial/sources.json
2. News collection, deduplication, AI summarization, and event matching
3. Conditional daily intelligence report generation and publishing (8:00 - 9:00 Tehran time)
4. Routine maintenance (gemini_request_log retention cleanup)
5. Clean, deterministic exit without long-running processes
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from sqlalchemy import select

# Ensure root directory is on sys.path and load .env if present
ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

env_file = ROOT_DIR / ".env"
if env_file.is_file():
    load_dotenv(env_file)

from app.collectors.source_manager import sync_sources  # noqa: E402
from app.config import settings  # noqa: E402
from app.database.connection import SessionLocal  # noqa: E402
from app.database.models import DailyReport  # noqa: E402
from app.processors.pipeline import collect_and_process  # noqa: E402
from app.scheduler.jobs import (  # noqa: E402
    cleanup_gemini_request_log,
    daily_report,
    reconcile_interrupted_jobs,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("run_once")


async def should_run_daily_report(
    force: bool = False,
    target_hour: int | None = None,
) -> tuple[bool, str]:
    """Determine whether daily report should be generated and published.

    Conditions:
    1. Check if today's report in configured timezone has already been published.
       If published with confirmed message IDs, returns (False, reason).
    2. If force is True: returns (True, reason).
    3. Check if current hour in configured timezone matches target_hour (default: 8 AM Tehran).
       If in window: returns (True, reason).
    4. Otherwise returns (False, reason).
    """
    tz = ZoneInfo(settings.timezone)
    now_tz = datetime.now(tz)
    today_date = now_tz.date()
    effective_hour = settings.daily_report_hour if target_hour is None else target_hour

    async with SessionLocal() as session:
        db_report = await session.scalar(
            select(DailyReport).where(DailyReport.report_date == today_date)
        )
        if (
            db_report is not None
            and db_report.status == "published"
            and bool(db_report.telegram_message_ids)
        ):
            msg_count = len(db_report.telegram_message_ids)
            return False, f"Daily report for {today_date} already published ({msg_count} messages)."

    if force:
        return True, "Forced via argument or environment variable."

    if now_tz.hour == effective_hour:
        return True, (
            f"Current hour ({now_tz.strftime('%H:%M')} {settings.timezone}) matches "
            f"daily report window ({effective_hour:02d}:00 - {effective_hour + 1:02d}:00)."
        )

    return False, (
        f"Current time ({now_tz.strftime('%H:%M')} {settings.timezone}) is outside "
        f"daily report window ({effective_hour:02d}:00 - {effective_hour + 1:02d}:00)."
    )


async def run_batch_pipeline(
    force_daily_report: bool = False,
    dry_run_daily_report: bool = False,
) -> int:
    """Execute the end-to-end batch processing pipeline.

    Returns exit code (0 on success, non-zero on error).
    """
    logger.info("=== Starting batch pipeline run ===")

    # 1. Synchronize sources
    logger.info("Step 1/4: Synchronizing news sources from editorial/sources.json...")
    await sync_sources()

    # Reconcile any interrupted jobs from previous runs
    await reconcile_interrupted_jobs()

    # 2. Collect and process new articles
    logger.info("Step 2/4: Collecting and processing new articles...")
    try:
        processed_count = await collect_and_process()
        logger.info("Successfully processed %d articles in this cycle.", processed_count)
    except Exception as exc:
        logger.exception("Error during news collection and processing: %s", exc)
        raise

    # 3. Check and publish daily report if due
    logger.info("Step 3/4: Checking daily report trigger...")
    should_publish, reason = await should_run_daily_report(force=force_daily_report)
    logger.info("Daily report decision: run=%s (%s)", should_publish, reason)

    if should_publish:
        logger.info("Generating and publishing daily intelligence report...")
        try:
            job_log = await daily_report(dry_run=dry_run_daily_report)
            logger.info(
                "Daily report finished with status '%s' (%d items sent).",
                job_log.status,
                job_log.processed_count or 0,
            )
        except Exception as exc:
            logger.exception("Error during daily report publishing: %s", exc)
            raise

    # 4. Clean up old logs
    logger.info("Step 4/4: Running routine maintenance...")
    await cleanup_gemini_request_log()

    logger.info("=== Batch pipeline run finished successfully ===")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Middle East News Bot - Single-Run Batch Execution"
    )
    parser.add_argument(
        "--force-daily-report",
        action="store_true",
        default=os.environ.get("FORCE_DAILY_REPORT", "").lower() in ("true", "1", "yes"),
        help="Force daily report generation regardless of current time",
    )
    parser.add_argument(
        "--dry-run-daily-report",
        action="store_true",
        default=os.environ.get("DRY_RUN_DAILY_REPORT", "").lower() in ("true", "1", "yes"),
        help="Execute report generation without sending real Telegram messages",
    )
    args = parser.parse_args()

    # Immediate per-article publishing must follow the same dry-run switch, otherwise a
    # test run would post real messages to the Telegram channel.
    settings.immediate_publish_dry_run = args.dry_run_daily_report

    exit_code = asyncio.run(
        run_batch_pipeline(
            force_daily_report=args.force_daily_report,
            dry_run_daily_report=args.dry_run_daily_report,
        )
    )
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
