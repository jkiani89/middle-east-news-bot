"""Scheduler jobs and lifecycle management for Middle East News Bot."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Coroutine
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.provider import (
    AIProviderError,
    AIRateLimitError,
    AIServiceUnavailableError,
    BaseAIProvider,
    GeminiProvider,
    is_retryable_ai_error,
)
from app.config import settings
from app.database.connection import SessionLocal
from app.database.models import Article, DailyReport, JobLog
from app.processors.pipeline import collect_new_articles, process_collected_articles
from app.reports.daily_report import (
    SLOT_CLAIMED,
    SLOT_IN_FLIGHT,
    SLOT_PUBLISHED,
    claim_slot,
    current_due_slot,
    generate_and_save_daily_report,
    generate_slot_bulletins,
    mark_slot_published,
    mark_slot_telegraph,
    release_slot,
    report_slot_hours,
    slot_candidates,
    slot_label,
    slot_report_date,
    slot_status,
)
from app.reports.telegraph_report import (
    TELEGRAM_FAILED,
    TELEGRAM_PENDING,
    TELEGRAM_SENT,
    TELEGRAPH_COMPLETED,
    TELEGRAPH_DISABLED,
    TELEGRAPH_FAILED,
    format_telegram_notification,
    notification_ready,
    publish_telegraph_report,
    record_telegram_status,
)
from app.telegram.publisher import MockPublishRefusedError, publish_report

logger = logging.getLogger(__name__)


def utcnow() -> datetime:
    """Return timezone-aware current UTC datetime."""
    return datetime.now(UTC)


async def count_pending_articles(session: AsyncSession) -> int:
    """Count articles waiting for AI processing (status == 'collected')."""
    stmt = select(func.count(Article.id)).where(Article.status == "collected")
    res = await session.scalar(stmt)
    return res or 0


async def _check_any_model_available_impl(
    make_provider: Callable[..., BaseAIProvider],
    primary_provider: BaseAIProvider | None = None,
    fallback_provider: BaseAIProvider | None = None,
    prefer_fallback: bool = True,
) -> bool:
    """Check if AT LEAST ONE model (primary or fallback) is available.

    Optimization (Option A):
    When fallback is enabled and prefer_fallback is True, pre-flight check tests the
    fallback model (e.g. gemini-3.5-flash-lite) first. This preserves 100% of the
    precious primary model quota (gemini-3.8-flash, 20 RPD) for real article analyses,
    since the fallback model operates on a much higher quota (1,500 RPD).
    If the fallback test succeeds, API connectivity and authentication are proven.
    If the fallback test fails, it falls back to checking the primary model.
    Skipping the hourly run only happens if BOTH models are unavailable.

    Transient failures (rate limit / 503 / timeout / network blip) are NOT treated as
    proof of unavailability: a flaky first probe or a single exhausted key must not
    stop the whole hourly run, because other keys in the rotator may still be healthy.
    """
    has_fallback = bool(settings.gemini_enable_fallback and settings.gemini_fallback_model)

    had_transient = False

    def _is_transient(exc: BaseException) -> bool:
        if isinstance(exc, (AIRateLimitError, AIServiceUnavailableError, TimeoutError)):
            return True
        return is_retryable_ai_error(exc)

    if prefer_fallback and has_fallback:
        if fallback_provider is None:
            fallback_provider = make_provider(
                primary_model=settings.gemini_fallback_model,
                enable_fallback=False,
            )
        try:
            res = await fallback_provider.generate_json('Respond with {"ok": true}', purpose="preflight")
            if res and res.get("ok") is True:
                logger.info(
                    "Pre-flight check passed on fallback model %s (Option A: 0 RPD consumed from primary %s).",
                    settings.gemini_fallback_model,
                    settings.gemini_primary_model,
                )
                return True
        except Exception as exc:  # noqa: BLE001 - classify and decide below
            if _is_transient(exc):
                had_transient = True
                logger.warning(
                    "Fallback model %s hit a transient pre-flight error (%s); "
                    "not treating this as unavailable, checking primary model...",
                    settings.gemini_fallback_model,
                    exc,
                )
            else:
                logger.warning("Fallback model pre-flight check error: %s", exc)

        if primary_provider is None:
            primary_provider = make_provider(
                primary_model=settings.gemini_primary_model,
                enable_fallback=False,
            )
        try:
            res = await primary_provider.generate_json('Respond with {"ok": true}', purpose="preflight")
            if res and res.get("ok") is True:
                logger.info("Primary model %s is available during pre-flight.", settings.gemini_primary_model)
                return True
        except Exception as exc:  # noqa: BLE001 - classify and decide below
            if _is_transient(exc):
                had_transient = True
                logger.warning(
                    "Primary model %s hit a transient pre-flight error (%s).",
                    settings.gemini_primary_model,
                    exc,
                )
            else:
                logger.warning("Primary model pre-flight check error: %s", exc)

        return had_transient

    else:
        if primary_provider is None:
            primary_provider = make_provider(
                primary_model=settings.gemini_primary_model,
                enable_fallback=False,
            )
        try:
            res = await primary_provider.generate_json('Respond with {"ok": true}', purpose="preflight")
            if res and res.get("ok") is True:
                return True
        except Exception as exc:  # noqa: BLE001 - classify and decide below
            if _is_transient(exc):
                had_transient = True
                logger.info("Primary model %s hit a transient pre-flight error (%s).", settings.gemini_primary_model, exc)
            else:
                logger.warning("Primary model pre-flight check error: %s", exc)

        if not has_fallback:
            return had_transient

        if fallback_provider is None:
            fallback_provider = make_provider(
                primary_model=settings.gemini_fallback_model,
                enable_fallback=False,
            )
        try:
            res = await fallback_provider.generate_json('Respond with {"ok": true}', purpose="preflight")
            if res and res.get("ok") is True:
                logger.info("Fallback model %s is available and healthy during pre-flight.", settings.gemini_fallback_model)
                return True
        except Exception as exc:  # noqa: BLE001 - classify and decide below
            if _is_transient(exc):
                had_transient = True
                logger.warning("Fallback model %s hit a transient pre-flight error (%s).", settings.gemini_fallback_model, exc)
            else:
                logger.warning("Fallback model pre-flight check error: %s", exc)

        return had_transient


async def check_any_model_available(
    primary_provider: BaseAIProvider | None = None,
    fallback_provider: BaseAIProvider | None = None,
    prefer_fallback: bool = True,
) -> bool:
    """Run the pre-flight check and close any provider created inside this call.

    Providers passed in by the caller are never closed here: their lifecycle
    belongs to the caller (pipeline, scheduler, tests).
    """
    transient_providers: list[BaseAIProvider] = []

    def make_provider(**kwargs: Any) -> BaseAIProvider:
        provider = GeminiProvider(**kwargs)
        transient_providers.append(provider)
        return provider

    try:
        return await _check_any_model_available_impl(
            make_provider,
            primary_provider=primary_provider,
            fallback_provider=fallback_provider,
            prefer_fallback=prefer_fallback,
        )
    finally:
        for provider in transient_providers:
            close = getattr(provider, "close", None)
            if close is None:
                continue
            try:
                close()
            except Exception:
                logger.warning("Failed to close a transient pre-flight provider", exc_info=True)


# Backward-compatible alias
check_primary_model_available = check_any_model_available


async def hourly_collection(
    provider: BaseAIProvider | None = None,
    health_checker: Callable[[], Coroutine[Any, Any, bool]] | None = None,
    pending_checker: Callable[[], Coroutine[Any, Any, int]] | None = None,
) -> JobLog:
    """Hourly: collect -> process -> event match. Does NOT publish.

    Ingestion always runs. The AI pre-flight and processing steps only run when
    articles are actually waiting, so an empty queue or a dead model never
    blocks the collection of new news.
    """
    async with SessionLocal() as session:
        job_log = JobLog(
            job_name="hourly_collection",
            started_at=utcnow(),
            status="running",
            processed_count=0,
        )
        session.add(job_log)
        await session.commit()
        await session.refresh(job_log)

        try:
            # Step 1: Ingestion must never depend on AI availability or queue state
            inserted_count = await collect_new_articles(session)
            logger.info("Ingested %d new articles from active feeds.", inserted_count)

            # Step 2: Quota-awareness - Check pending articles BEFORE making any Gemini pre-flight call
            pending_count: int
            if pending_checker is not None:
                pending_count = await pending_checker()
            else:
                pending_count = await count_pending_articles(session)

            if pending_count == 0:
                logger.info("No pending articles to process. Skipping hourly AI cycle (0 Gemini calls made).")
                job_log.status = "skipped_no_pending"
                return job_log

            # Step 3: Pre-flight check across all available models (Bug 1 fix)
            is_healthy: bool = False
            if health_checker is not None:
                is_healthy = await health_checker()
            else:
                is_healthy = await check_any_model_available()

            if not is_healthy:
                logger.warning("Both primary and fallback models unavailable during pre-flight. Skipping this hourly cycle.")
                job_log.status = "skipped_all_models_unavailable"
                return job_log

            # Step 4: AI processing of the queued articles
            processed_count = await process_collected_articles(
                session, limit=settings.max_articles_per_batch, provider=provider
            )
            job_log.processed_count = processed_count
            job_log.status = "success"

        except AIRateLimitError as exc:
            logger.error("Rate limit/quota exceeded during hourly collection: %s", exc)
            job_log.status = "failed_quota"
            job_log.error_message = str(exc)[:2000]
        except AIServiceUnavailableError as exc:
            logger.error("AI service unavailable during hourly collection: %s", exc)
            job_log.status = "failed_unavailable"
            job_log.error_message = str(exc)[:2000]
        except AIProviderError as exc:
            logger.error("AI Provider error during hourly collection: %s", exc)
            job_log.status = "failed_ai"
            job_log.error_message = str(exc)[:2000]
        except Exception as exc:
            logger.exception("Unexpected error in hourly_collection: %s", exc)
            job_log.status = "failed"
            job_log.error_message = str(exc)[:2000]
        finally:
            job_log.finished_at = utcnow()
            await session.commit()

        return job_log


def _dump_dry_run_briefing(
    report_date: date,
    label: str,
    text: str,
    items: list[dict[str, Any]],
    metadata: dict[str, Any],
    notification: str | None = None,
) -> None:
    """Write the dry-run bulletin text so a test run leaves inspectable evidence.

    Without this, a dry run only logs message counts: nothing of what the reader would
    have received survives, so the narrative matrix cannot be reviewed before a real
    send. A failure here is logged and swallowed, because evidence collection must
    never change the outcome of the run itself.
    """
    try:
        out_dir = Path("scratch")
        out_dir.mkdir(parents=True, exist_ok=True)
        stem = f"dry_run_briefing_{report_date.isoformat()}_{label.replace(':', '')}"
        (out_dir / f"{stem}.txt").write_text(text, encoding="utf-8")
        if notification:
            (out_dir / f"{stem}.telegram.txt").write_text(notification, encoding="utf-8")
        if metadata.get("telegraph_index_url"):
            (out_dir / f"{stem}.telegraph.txt").write_text(
                str(metadata["telegraph_index_url"]), encoding="utf-8"
            )
        logger.info("Dry-run evidence written to %s/%s.txt", out_dir, stem)
    except Exception:
        logger.exception("Could not write dry-run evidence files; the run outcome is unaffected.")


async def publish_slot_briefing(
    session: AsyncSession,
    slot_hour: int,
    bot: Any | None = None,
    dry_run: bool = False,
    report_date: date | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """Publish one 12-hour report: Telegraph pages first, then the Telegram notification.

    The two channels have independent state. Telegraph pages are recorded per slot, so a
    retry after a failed notification reuses every existing page and only repeats the
    send. A notification that already succeeded is never sent twice.
    """
    rep_date = report_date or slot_report_date(slot_hour)
    label = slot_label(slot_hour)

    state = await slot_status(session, rep_date, slot_hour) or {}
    if not force and state.get("status") == SLOT_PUBLISHED:
        logger.info("Briefing %s for %s was already delivered; skipping.", label, rep_date)
        return {
            "messages": 0,
            "telegram_status": TELEGRAM_SENT,
            "telegraph_status": None,
            "skipped": "published",
            "error": None,
        }
    if not force and state.get("status") == SLOT_IN_FLIGHT:
        logger.info(
            "Briefing %s for %s is in flight in another run (claimed at %s); skipping.",
            label,
            rep_date,
            state.get("claimed_at"),
        )
        return {
            "messages": 0,
            "telegram_status": "in_flight",
            "telegraph_status": None,
            "skipped": "in_flight",
            "error": None,
        }

    rep_date, sections, text, metadata = await generate_slot_bulletins(
        report_date=rep_date, slot_hour=slot_hour
    )
    items = [item for _key, _name, topic_items in sections for item in topic_items]

    if not items:
        logger.info("Briefing %s for %s has no events; nothing to publish.", label, rep_date)
        if not dry_run:
            # Record the slot so the recovery pass does not re-run an empty window forever.
            await mark_slot_published(session, rep_date, slot_hour, [])
        return {
            "messages": 0,
            "telegram_status": "empty",
            "telegraph_status": None,
            "skipped": "empty",
            "error": None,
        }

    # Another run may be delivering this slot right now. The claim is written before the
    # first external call, so it also covers a run that died mid-delivery: after the
    # timeout the next run is allowed to retry instead of the slot being stuck forever.
    if not dry_run and not force:
        outcome = await claim_slot(session, rep_date, slot_hour)
        if outcome == SLOT_PUBLISHED:
            logger.info("Briefing %s for %s was delivered by another run; skipping.", label, rep_date)
            return {
                "messages": 0,
                "telegram_status": TELEGRAM_SENT,
                "telegraph_status": None,
                "skipped": "published",
                "error": None,
            }
        if outcome != SLOT_CLAIMED:
            logger.info(
                "Briefing %s for %s could not be claimed (%s); another run holds it.",
                label,
                rep_date,
                outcome,
            )
            return {
                "messages": 0,
                "telegram_status": "in_flight",
                "telegraph_status": None,
                "skipped": outcome,
                "error": None,
            }

    content_quality = metadata.get("content_quality", "real")

    # 1) Telegraph: the full report lives here. Failure here is recorded, never masked by
    #    a successful Telegram send.
    try:
        telegraph = await publish_telegraph_report(
            session,
            report_date=rep_date,
            slot_hour=slot_hour,
            metadata=metadata,
            dry_run=dry_run,
            force=force,
            persist=not dry_run,
        )
    except Exception as exc:
        logger.exception("Telegraph publication failed for briefing %s of %s.", label, rep_date)
        telegraph = {
            "status": TELEGRAPH_FAILED,
            "index_url": "",
            "index_path": "",
            "failures": [{"scope": "publish", "error": str(exc)}],
            "window_start": None,
            "window_end": None,
        }

    telegraph_status = str(telegraph.get("status") or TELEGRAPH_FAILED)

    # 2) Telegram: a notification that links to the report index (the full text is the
    #    fallback when Telegraph is switched off by the operator).
    notification_sections = telegraph.get("sections") or sections
    if telegraph_status == TELEGRAPH_DISABLED:
        report_text = text
    else:
        report_text = format_telegram_notification(
            rep_date,
            slot_hour,
            str(telegraph.get("index_url") or telegraph.get("index_path") or ""),
            notification_sections,
            {
                **metadata,
                "window_start": telegraph.get("window_start"),
                "window_end": telegraph.get("window_end"),
            },
            topic_pages=telegraph.get("pages") or {},
        )

    messages = 0
    message_ids: list[int] = []
    telegram_status = TELEGRAM_PENDING
    error: str | None = None
    ready = notification_ready(telegraph) or telegraph_status == TELEGRAPH_DISABLED
    if dry_run and telegraph_status == TELEGRAPH_COMPLETED:
        ready = True

    if not ready:
        telegram_status = TELEGRAM_FAILED
        error = f"report index unavailable (telegraph status: {telegraph_status})"
        logger.error("Skipping the Telegram notification for %s of %s: %s", label, rep_date, error)
    else:
        try:
            sent = await publish_report(
                report_date=rep_date,
                items=items,
                content_quality=content_quality,
                report_text=report_text,
                bot=bot,
                dry_run=dry_run,
                persist=False,
            )
            message_ids = [entry["message_id"] for entry in sent if entry.get("message_id")]
            messages = len(sent)
            telegram_status = TELEGRAM_SENT
        except MockPublishRefusedError as exc:
            telegram_status = TELEGRAM_FAILED
            error = str(exc)
            logger.warning("Telegram notification refused for %s of %s: %s", label, rep_date, exc)
        except Exception as exc:
            logger.exception("Telegram notification failed for %s of %s.", label, rep_date)
            telegram_status = TELEGRAM_FAILED
            error = str(exc)

    if dry_run:
        _dump_dry_run_briefing(
            rep_date,
            label,
            text,
            items,
            {
                **metadata,
                "telegraph_index_url": telegraph.get("index_url"),
                "telegraph_status": telegraph_status,
                "telegram_status": telegram_status,
            },
            notification=report_text,
        )
        return {
            "messages": messages,
            "telegram_status": telegram_status,
            "telegraph_status": telegraph_status,
            "skipped": None,
            "error": error,
            "telegraph_index_url": telegraph.get("index_url"),
        }

    await mark_slot_telegraph(
        session, rep_date, slot_hour, telegraph.get("index_url"), telegraph_status
    )

    if telegram_status == TELEGRAM_SENT:
        await record_telegram_status(
            session,
            rep_date,
            slot_hour,
            TELEGRAM_SENT,
            message_ids[0] if message_ids else None,
        )
        await mark_slot_published(session, rep_date, slot_hour, message_ids)
    else:
        await record_telegram_status(
            session, rep_date, slot_hour, TELEGRAM_FAILED, None, error
        )
        # Release the claim: the next run retries the notification only, because the
        # Telegraph pages are already recorded and are reused.
        await release_slot(session, rep_date, slot_hour)

    logger.info(
        "Briefing %s for %s: %d event(s) in %d section(s), telegraph=%s, telegram=%s.",
        label,
        rep_date,
        len(items),
        len(sections),
        telegraph_status,
        telegram_status,
    )
    return {
        "messages": messages,
        "telegram_status": telegram_status,
        "telegraph_status": telegraph_status,
        "skipped": None,
        "error": error,
        "telegraph_index_url": telegraph.get("index_url"),
    }


async def _publish_slot_briefing(
    session: AsyncSession,
    slot_hour: int,
    bot: Any | None,
    dry_run: bool,
    report_date: date | None = None,
    force: bool = False,
) -> int:
    """Backward-compatible wrapper returning the number of Telegram messages sent."""
    outcome = await publish_slot_briefing(
        session,
        slot_hour=slot_hour,
        bot=bot,
        dry_run=dry_run,
        report_date=report_date,
        force=force,
    )
    return int(outcome.get("messages") or 0)


async def daily_report(
    bot: Any | None = None,
    dry_run: bool = False,
    slot_hour: int | None = None,
    report_date: date | None = None,
) -> JobLog:
    """Daily report generation and publishing.

    With ``slot_hour`` this publishes the 12-hour report of that slot (Telegraph pages
    plus the Telegram notification). ``report_date`` names the day the slot started, which
    is what recovery needs for a slot that was missed. Without ``slot_hour`` the legacy
    24-hour text report is produced unchanged.
    """
    async with SessionLocal() as session:
        job_log = JobLog(
            job_name="daily_report",
            started_at=utcnow(),
            status="running",
            processed_count=0,
        )
        session.add(job_log)
        await session.commit()
        await session.refresh(job_log)

        try:
            if slot_hour is not None:
                outcome = await publish_slot_briefing(
                    session=session,
                    slot_hour=slot_hour,
                    bot=bot,
                    dry_run=dry_run,
                    report_date=report_date,
                )
                job_log.processed_count = int(outcome.get("messages") or 0)
                if outcome.get("telegram_status") == TELEGRAM_FAILED:
                    job_log.status = "failed"
                    job_log.error_message = str(outcome.get("error") or "telegram delivery failed")[:2000]
                else:
                    job_log.status = "success"
                return job_log

            rep_date, items, text = await generate_and_save_daily_report()
            # Query stored report to extract content_quality
            db_report = await session.scalar(
                select(DailyReport).where(DailyReport.report_date == rep_date)
            )
            content_quality = "real"
            if db_report and db_report.content:
                content_quality = db_report.content.get("content_quality", "real")

            # Publish report to Telegram
            sent = await publish_report(
                report_date=rep_date,
                items=items,
                content_quality=content_quality,
                bot=bot,
                dry_run=dry_run,
                session=session,
            )
            job_log.processed_count = len(sent) if sent else 0
            job_log.status = "success"

        except MockPublishRefusedError as exc:
            logger.warning("Daily report publishing skipped due to unverified content: %s", exc)
            job_log.status = "skipped_mock_content"
            job_log.error_message = str(exc)[:2000]
        except Exception as exc:
            logger.exception("Unexpected error in daily_report: %s", exc)
            job_log.status = "failed"
            job_log.error_message = str(exc)[:2000]
        finally:
            job_log.finished_at = utcnow()
            await session.commit()

        return job_log


async def reconcile_interrupted_jobs(max_age_minutes: int = 30) -> int:
    """At startup: mark any 'running' jobs older than max_age_minutes as 'interrupted'."""
    cutoff = utcnow() - timedelta(minutes=max_age_minutes)
    async with SessionLocal() as session:
        stmt = (
            update(JobLog)
            .where(JobLog.status == "running", JobLog.started_at < cutoff)
            .values(status="interrupted", finished_at=utcnow())
        )
        result = await session.execute(stmt)
        await session.commit()
        count = result.rowcount
        if count > 0:
            logger.info("Reconciled %d interrupted jobs at startup.", count)
        return count


async def cleanup_heavy_columns() -> None:
    """Daily: null heavy columns (original_text, ai_payload) for articles older than 7 days.

    Keeps the row, title and content_hash intact so duplicate detection and
    reporting history remain valid. Only touches rows that still carry heavy
    data, to avoid extra WAL churn.

    Skipped in APP_ENV=test.
    """
    if settings.app_env == "test":
        logger.debug("Skipping cleanup_heavy_columns in test environment")
        return

    from datetime import timedelta

    from sqlalchemy import update

    from app.database.connection import SessionLocal
    from app.database.models import Article

    cutoff = datetime.now(UTC) - timedelta(days=7)
    batch_size = 1000
    total_cleaned = 0
    try:
        async with SessionLocal() as session:
            while True:
                id_stmt = (
                    select(Article.id)
                    .where(
                        Article.collected_at < cutoff,
                        (Article.original_text.is_not(None)) | (Article.ai_payload.is_not(None)),
                    )
                    .limit(batch_size)
                )
                ids = (await session.scalars(id_stmt)).all()
                if not ids:
                    break
                stmt = (
                    update(Article)
                    .where(Article.id.in_(ids))
                    .values(original_text=None, ai_payload=None)
                )
                result = await session.execute(stmt)
                await session.commit()
                cleaned = result.rowcount if isinstance(result.rowcount, int) else 0
                total_cleaned += cleaned
                if len(ids) < batch_size:
                    break
            logger.info(
                "Cleaned heavy columns for %d articles older than 7 days (cutoff=%s)",
                total_cleaned,
                cutoff.isoformat(),
            )
    except Exception:
        logger.exception("cleanup_heavy_columns failed")


async def cleanup_gemini_request_log() -> None:
    """Daily: delete old rows from gemini_request_log to prevent unbounded growth.

    Retention: settings.gemini_log_retention_days (default 30 days).
    Skipped in APP_ENV=test to keep tests isolated and fast.

    Runs in-process via psycopg (sync). For 30-day DELETE with proper
    index, expected < 1s. Future: move to asyncio.to_thread if it grows.
    """
    if settings.app_env == "test":
        logger.debug("Skipping cleanup_gemini_request_log in test environment")
        return

    # Import inside function to avoid circular imports at module load
    from app.ai.postgres_adapter import PostgresAdapter
    from app.ai.provider import GeminiProvider

    adapter: PostgresAdapter | None = None
    try:
        dsn = GeminiProvider._convert_dsn(settings.database_url)
        adapter = PostgresAdapter(dsn)
        cutoff = time.time() - settings.gemini_log_retention_days * 86400
        cur = adapter._ensure().execute(
            "DELETE FROM gemini_request_log WHERE ts < %s",
            (cutoff,),
        )
        logger.info(
            "Cleaned %d rows from gemini_request_log (retention=%d days, cutoff_ts=%.0f)",
            cur.rowcount,
            settings.gemini_log_retention_days,
            cutoff,
        )
    except Exception:
        logger.exception("cleanup_gemini_request_log failed")
    finally:
        if adapter is not None:
            adapter.close()


async def recover_missed_briefings(
    now: datetime | None = None,
    bot: Any | None = None,
    dry_run: bool = False,
    exclude: set[tuple[date, int]] | None = None,
) -> list[dict[str, Any]]:
    """Publish slots that a dropped scheduled run never delivered.

    ``exclude`` carries the slot this run is already publishing, so a report is never
    treated as missed while the current run is producing it. Slots with a live claim are
    skipped too, and published slots cost only one cheap status read: they are checked
    before any report generation (and before any AI backfill call).
    """
    excluded = set(exclude or set())
    budget = max(0, settings.max_recovery_slots_per_run)
    recovered: list[dict[str, Any]] = []
    if budget == 0:
        return recovered

    for rep_date, slot_hour in slot_candidates(now=now):
        if budget == 0:
            break
        if (rep_date, slot_hour) in excluded:
            continue
        async with SessionLocal() as session:
            state = await slot_status(session, rep_date, slot_hour) or {}
        status = state.get("status")
        if status in (SLOT_PUBLISHED, SLOT_IN_FLIGHT):
            continue
        logger.warning(
            "Recovering missed briefing %s for %s (slot status: %s).",
            slot_label(slot_hour),
            rep_date,
            status or "missing",
        )
        job_log = await daily_report(
            slot_hour=slot_hour, report_date=rep_date, bot=bot, dry_run=dry_run
        )
        recovered.append(
            {
                "report_date": rep_date,
                "slot_hour": slot_hour,
                "status": job_log.status,
                "error": job_log.error_message,
            }
        )
        budget -= 1
    return recovered


async def run_scheduled_briefings(
    bot: Any | None = None,
    dry_run: bool = False,
    now: datetime | None = None,
) -> list[JobLog]:
    """Publish the slot that just came due, then recover any older missed slot.

    The current slot is excluded from the recovery pass, so a report is never both
    produced by this run and treated as missed by it. Slots that are already published
    or claimed are skipped before any report generation happens.
    """
    moment = now or datetime.now(ZoneInfo(settings.timezone))
    logs: list[JobLog] = []
    due = current_due_slot(moment)
    if due is not None:
        rep_date, slot_hour = due
        logs.append(
            await daily_report(
                slot_hour=slot_hour, report_date=rep_date, bot=bot, dry_run=dry_run
            )
        )
    else:
        logger.info("No briefing slot is currently inside its catch-up window.")
    await recover_missed_briefings(now=moment, bot=bot, dry_run=dry_run, exclude={due} if due else set())
    return logs


async def _scheduled_briefing() -> list[JobLog]:
    """APScheduler entrypoint: the due slot plus any missed slot."""
    return await run_scheduled_briefings()


def build_scheduler() -> AsyncIOScheduler:
    """Build and configure APScheduler instance."""
    scheduler = AsyncIOScheduler(timezone=ZoneInfo(settings.timezone))
    scheduler.add_job(
        hourly_collection,
        trigger=IntervalTrigger(minutes=settings.collection_interval_minutes),
        id="hourly_collection",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=300,
    )
    scheduler.add_job(
        _scheduled_briefing,
        trigger=CronTrigger(
            hour=",".join(str(hour) for hour in report_slot_hours()),
            minute=0,
        ),
        id="daily_report",
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        cleanup_gemini_request_log,
        trigger=CronTrigger(hour=3, minute=0),
        id="cleanup_gemini_request_log",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=600,
    )
    scheduler.add_job(
        cleanup_heavy_columns,
        trigger=CronTrigger(hour=3, minute=30),
        id="cleanup_heavy_columns",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=600,
    )
    return scheduler


# Backward-compatible function aliases
hourly_job = hourly_collection
daily_job = daily_report
