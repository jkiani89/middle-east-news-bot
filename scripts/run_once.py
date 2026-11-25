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
import faulthandler
import io
import logging
import os
import sys
import threading
import traceback
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

from app.ai.provider import flush_gemini_audit  # noqa: E402
from app.collectors.source_manager import sync_sources  # noqa: E402
from app.config import settings  # noqa: E402
from app.database.connection import SessionLocal, engine  # noqa: E402
from app.database.models import DailyReport  # noqa: E402
from app.notifications.alerts import maybe_send_admin_alert  # noqa: E402
from app.processors.pipeline import collect_and_process  # noqa: E402
from app.reports.daily_report import (  # noqa: E402
    current_due_slot,
    current_slot_hour,
    latest_slot_start,
    slot_label,
    slot_status,
)
from app.scheduler.jobs import (  # noqa: E402
    cleanup_gemini_request_log,
    cleanup_heavy_columns,
    daily_report,
    reconcile_interrupted_jobs,
    run_scheduled_briefings,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("run_once")

# Ceiling for the audit rows still queued on the provider's daemon writer thread.
AUDIT_FLUSH_TIMEOUT_SECONDS = 5.0

# Interval at which the asyncio watchdog names every still-pending coroutine and its
# last await point. A pure-async stall (loop in select, all threads idle) leaves no
# other trace, so this is the only way to learn which coroutine is parked on an await.
ASYNCIO_WATCHDOG_INTERVAL_SECONDS = 120.0


def local_now(tz: ZoneInfo | None = None) -> datetime:
    """Current time in the configured timezone (patchable in tests)."""
    return datetime.now(tz or ZoneInfo(settings.timezone))


async def should_run_daily_report(
    force: bool = False,
    target_hour: int | None = None,
    slot_hour: int | None = None,
) -> tuple[bool, str]:
    """Determine whether daily report should be generated and published.

    Conditions:
    1. Check if today's report in configured timezone has already been published.
       If published with confirmed message IDs, returns (False, reason).
    2. If force is True: returns (True, reason).
    3. Check if current time is inside the slot's catch-up window: from the slot hour
       until ``settings.slot_grace_hours`` later (default 8-11 and 20-23 Tehran).
       A scheduled run that GitHub delays or queues late still publishes its bulletin.
    4. Otherwise returns (False, reason).
    """
    tz = ZoneInfo(settings.timezone)
    now_tz = local_now(tz)
    # A caller that names only the slot must still get that slot: falling back to the
    # legacy daily hour would silently check the 08:00 window for every slot.
    if target_hour is not None:
        effective_hour = target_hour
    elif slot_hour is not None:
        effective_hour = slot_hour
    else:
        effective_hour = settings.daily_report_hour

    # Phase Y-10: a slot that started yesterday keeps yesterday's report_date for the
    # claim, the window and the dedup check, so its bulletin is not mistaken for today's.
    slot_start = latest_slot_start(now_tz, effective_hour) if slot_hour is not None else None
    target_date = slot_start.date() if slot_start is not None else now_tz.date()

    async with SessionLocal() as session:
        db_report = await session.scalar(
            select(DailyReport).where(DailyReport.report_date == target_date)
        )

        if slot_hour is not None:
            label = slot_label(slot_hour)
            bulletins = (db_report.content or {}).get("bulletins", {}) if db_report else {}
            record = bulletins.get(label) or {}
            state = await slot_status(session, target_date, slot_hour) or {}
            if state.get("status") == "published" or (
                record.get("status") == "published" and record.get("message_ids")
            ):
                return False, f"Briefing {label} for {target_date} already published."
            if state.get("status") == "in_flight":
                return False, (
                    f"Briefing {label} for {target_date} is in flight in another run "
                    f"(claimed at {state.get('claimed_at')})."
                )

        if (
            slot_hour is None
            and db_report is not None
            and db_report.status == "published"
            and bool(db_report.telegram_message_ids)
        ):
            msg_count = len(db_report.telegram_message_ids)
            return False, f"Daily report for {target_date} already published ({msg_count} messages)."

    if force:
        return True, "Forced via argument or environment variable."

    # The settings validator already clamps this to 1..11; repeat the guard here so a
    # directly-mutated setting in a test or script cannot produce a zero-length window.
    grace_hours = min(max(1, settings.slot_grace_hours), 11)
    now_minutes = now_tz.hour * 60 + now_tz.minute
    window_start = effective_hour * 60
    window_end = window_start + grace_hours * 60

    start_label = f"{window_start // 60:02d}:{window_start % 60:02d}"
    if window_end <= 24 * 60:
        window_label = f"{start_label} - {window_end // 60:02d}:{window_end % 60:02d}"
    else:
        carry_hour = (window_end - 24 * 60) // 60
        window_label = f"{start_label} - {carry_hour:02d}:{window_end % 60:02d} (+1d)"

    # Phase Y-10: measure from the slot's real start, so a window that crosses
    # midnight stays open for the slot that began yesterday (00:42 is 4h42 after
    # yesterday's 20:00, not 20h42 before today's).
    if slot_start is not None:
        minutes_since_window = (now_tz - slot_start).total_seconds() / 60
        inside_window = 0 <= minutes_since_window < grace_hours * 60
    else:
        # Modulo keeps a window that crosses midnight working instead of ending at 24:00.
        minutes_since_window = (now_minutes - window_start) % (24 * 60)
        inside_window = minutes_since_window < grace_hours * 60

    logger.info(
        "Slot decision: slot=%s, now=%s, grace=%sh -> publish=%s",
        effective_hour,
        now_tz.strftime("%H:%M"),
        grace_hours,
        inside_window,
    )

    if inside_window:
        return True, (
            f"Current time ({now_tz.strftime('%H:%M')} {settings.timezone}) matches "
            f"the {effective_hour:02d}:00 slot inside its catch-up window ({window_label})."
        )

    return False, (
        f"Current time ({now_tz.strftime('%H:%M')} {settings.timezone}) is outside "
        f"the catch-up window of the {effective_hour:02d}:00 slot ({window_label})."
    )


async def _asyncio_task_watchdog() -> None:
    """Log every pending task and its last three stack frames every 120 seconds.

    Runs as a background task for the whole batch. It is cancelled (and awaited)
    in ``run_batch_pipeline``'s ``finally`` block, so it never outlives the run.
    """
    try:
        while True:
            await asyncio.sleep(ASYNCIO_WATCHDOG_INTERVAL_SECONDS)
            pending = [task for task in asyncio.all_tasks() if not task.done()]
            if not pending:
                continue
            logger.warning(
                "Asyncio watchdog: %d pending task(s) after %.0fs without progress.",
                len(pending),
                ASYNCIO_WATCHDOG_INTERVAL_SECONDS,
            )
            for task in pending:
                logger.warning("  pending task: %s", task.get_name())
                try:
                    buf = io.StringIO()
                    task.print_stack(file=buf)
                    stack_text = buf.getvalue().rstrip()
                except Exception:  # noqa: BLE001 - a finished task has no stack
                    stack_text = ""
                if stack_text:
                    for line in stack_text.splitlines():
                        logger.warning("    %s", line)
                else:
                    logger.warning("    (no stack available)")
    except asyncio.CancelledError:
        raise


async def run_batch_pipeline(
    force_daily_report: bool = False,
    dry_run_daily_report: bool = False,
    slot_hour: int | None = None,
    scheduled_briefings: bool = False,
) -> int:
    """Execute the end-to-end batch processing pipeline.

    With ``scheduled_briefings`` the run publishes the slot that just came due and then
    recovers any slot a dropped scheduled run missed. Naming ``slot_hour`` manually
    publishes exactly that slot and never runs recovery, so a manual run cannot pick a
    different window.

    Returns exit code (0 on success, non-zero on error).
    """
    watchdog: asyncio.Task[None] | None = None
    try:
        logger.info("=== Starting batch pipeline run ===")
        watchdog = asyncio.create_task(
            _asyncio_task_watchdog(), name="asyncio-task-watchdog"
        )

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

        # 3. Check and publish the report of the due slot (and recover missed ones)
        logger.info("Step 3/4: Checking scheduled briefing trigger...")
        if scheduled_briefings:
            logger.info("Publishing the due briefing and recovering missed slots...")
            try:
                job_logs = await run_scheduled_briefings(dry_run=dry_run_daily_report)
                for job_log in job_logs:
                    logger.info(
                        "Briefing job finished with status '%s' (%d messages).",
                        job_log.status,
                        job_log.processed_count or 0,
                    )
            except Exception as exc:
                logger.exception("Error during scheduled briefing publishing: %s", exc)
                raise
        else:
            should_publish, reason = await should_run_daily_report(
                force=force_daily_report, target_hour=slot_hour, slot_hour=slot_hour
            )
            logger.info("Daily report decision: run=%s (%s)", should_publish, reason)

            if should_publish:
                logger.info("Generating and publishing the report...")
                try:
                    if slot_hour is None:
                        job_log = await daily_report(dry_run=dry_run_daily_report)
                    else:
                        job_log = await daily_report(
                            dry_run=dry_run_daily_report, slot_hour=slot_hour
                        )
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
        await cleanup_heavy_columns()

        logger.info("=== Batch pipeline run finished successfully ===")
        return 0
    finally:
        if watchdog is not None and not watchdog.done():
            watchdog.cancel()
            try:
                await watchdog
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        try:
            await maybe_send_admin_alert()
        except Exception:  # noqa: BLE001
            logger.exception("Admin alert check failed; continuing.")
        # Nothing may outlive this coroutine: asyncio.run() closes the loop right
        # after it returns, so a connection still sitting in the pool would be torn
        # down uncleanly while Aiven keeps counting it against the free-tier limit.
        try:
            await engine.dispose()
            logger.info("Database engine disposed; pooled connections released.")
        except Exception:  # noqa: BLE001
            logger.exception("Failed to dispose the database engine; continuing.")


def _lingering_non_daemon_threads() -> list[threading.Thread]:
    """Non-daemon threads that would otherwise keep the interpreter alive."""
    current = threading.current_thread()
    return [
        thread
        for thread in threading.enumerate()
        if thread is not current and thread.is_alive() and not thread.daemon
    ]


def main() -> None:
    # A stall inside a step leaves no log line at all (run #244 sat after
    # "Cleaned 0 rows from gemini_request_log" for ~8 minutes). Dump every
    # thread's stack after three minutes of silence, repeatedly, so the next hang
    # names its own blocking call in the workflow log.
    faulthandler.dump_traceback_later(180, repeat=True)

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
    parser.add_argument(
        "--slot-hour",
        type=int,
        default=None,
        help="Publish the scheduled briefing of this local hour (default: legacy 24h report)",
    )
    args = parser.parse_args()

    slot_hour = args.slot_hour
    if slot_hour is None:
        env_slot = os.environ.get("REPORT_SLOT_HOUR", "").strip()
        if env_slot.isdigit():
            slot_hour = int(env_slot)
    scheduled_briefings = False
    if slot_hour is None and os.environ.get("SCHEDULED_BRIEFINGS", "").lower() in (
        "true",
        "1",
        "yes",
    ):
        # The scheduled path resolves the due slot itself and then recovers missed ones.
        scheduled_briefings = True

    if scheduled_briefings and args.force_daily_report:
        # A forced manual run publishes the slot that is due right now and skips the
        # recovery pass, so forcing cannot re-publish historical slots.
        due = current_due_slot()
        slot_hour = due[1] if due else current_slot_hour()
        scheduled_briefings = False

    # Immediate per-article publishing must follow the same dry-run switch, otherwise a
    # test run would post real messages to the Telegram channel.
    settings.immediate_publish_dry_run = args.dry_run_daily_report

    exit_code = 0
    error: BaseException | None = None
    # asyncio.run() would call Runner.close(), which joins the default executor for up to
    # asyncio.constants.THREAD_JOIN_TIMEOUT (300 seconds in 3.12+). A google-genai call
    # that outlived its ``wait_for`` keeps running on such a worker, so the process hung
    # for minutes after "Step 4/4" while the batch was already fully persisted. The loop is
    # driven explicitly and the process leaves through os._exit instead of that join.
    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        exit_code = loop.run_until_complete(
            run_batch_pipeline(
                force_daily_report=args.force_daily_report,
                dry_run_daily_report=args.dry_run_daily_report,
                slot_hour=slot_hour,
                scheduled_briefings=scheduled_briefings,
            )
        )
    except BaseException as exc:  # noqa: BLE001 - reported below, then exit code 1
        error = exc
        exit_code = 1
    finally:
        # Audit rows are queued on a daemon thread; give them a bounded chance to land.
        try:
            pending = flush_gemini_audit(AUDIT_FLUSH_TIMEOUT_SECONDS)
            if pending:
                logger.warning("%d gemini_usage row(s) were still queued at exit.", pending)
        except Exception:  # noqa: BLE001
            logger.exception("Audit flush failed; continuing.")
        # Handlers keep buffered records; flush them before any hard exit.
        logging.shutdown()

    if error is not None:
        traceback.print_exception(type(error), error, error.__traceback__)

    sys.stdout.flush()
    sys.stderr.flush()
    lingering = _lingering_non_daemon_threads()
    if lingering:
        print(
            f"Exiting with {len(lingering)} background thread(s) still alive; "
            "forcing process exit.",
            file=sys.stderr,
            flush=True,
        )
    if lingering or error is not None:
        # os._exit skips the interpreter-exit join of those workers (and the executor
        # shutdown asyncio.run would perform), so the workflow step ends immediately.
        os._exit(exit_code)

    loop.close()
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
