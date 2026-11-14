"""Scheduler jobs and lifecycle management for Middle East News Bot."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Coroutine
from datetime import UTC, datetime, timedelta
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
)
from app.config import settings
from app.database.connection import SessionLocal
from app.database.models import Article, DailyReport, JobLog
from app.processors.pipeline import collect_new_articles, process_collected_articles
from app.reports.daily_report import generate_and_save_daily_report
from app.telegram.publisher import (
    MockPublishRefusedError,
    publish_daily_report_pdf,
    publish_report,
)

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
    """
    has_fallback = bool(settings.gemini_enable_fallback and settings.gemini_fallback_model)

    if prefer_fallback and has_fallback:
        # 1. Test fallback model first to save primary 20 RPD quota
        if fallback_provider is None:
            fallback_provider = make_provider(
                primary_model=settings.gemini_fallback_model,
                enable_fallback=False,  # Test fallback directly
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
        except (AIRateLimitError, AIServiceUnavailableError) as exc:
            logger.warning(
                "Fallback model %s unavailable during pre-flight (%s). Checking primary model...",
                settings.gemini_fallback_model,
                exc,
            )
        except Exception as exc:
            logger.warning("Fallback model pre-flight check error: %s", exc)

        # 2. Fallback failed, test primary model as second chance
        if primary_provider is None:
            primary_provider = make_provider(
                primary_model=settings.gemini_primary_model,
                enable_fallback=False,  # Test only primary
            )
        try:
            res = await primary_provider.generate_json('Respond with {"ok": true}', purpose="preflight")
            if res and res.get("ok") is True:
                logger.info("Primary model %s is available during pre-flight.", settings.gemini_primary_model)
                return True
        except (AIRateLimitError, AIServiceUnavailableError):
            logger.warning("Primary model %s also unavailable during pre-flight.", settings.gemini_primary_model)
        except Exception as exc:
            logger.warning("Primary model pre-flight check error: %s", exc)

        return False

    else:
        # Fallback disabled or prefer_fallback=False: Test primary first
        if primary_provider is None:
            primary_provider = make_provider(
                primary_model=settings.gemini_primary_model,
                enable_fallback=False,  # Test only primary
            )
        try:
            res = await primary_provider.generate_json('Respond with {"ok": true}', purpose="preflight")
            if res and res.get("ok") is True:
                return True
        except (AIRateLimitError, AIServiceUnavailableError):
            logger.info("Primary model %s unavailable during pre-flight. Checking fallback...", settings.gemini_primary_model)
        except Exception as exc:
            logger.warning("Primary model pre-flight check error: %s", exc)

        if not has_fallback:
            return False

        if fallback_provider is None:
            fallback_provider = make_provider(
                primary_model=settings.gemini_fallback_model,
                enable_fallback=False,  # Test fallback directly
            )
        try:
            res = await fallback_provider.generate_json('Respond with {"ok": true}', purpose="preflight")
            if res and res.get("ok") is True:
                logger.info("Fallback model %s is available and healthy during pre-flight.", settings.gemini_fallback_model)
                return True
        except (AIRateLimitError, AIServiceUnavailableError):
            logger.warning("Fallback model %s also unavailable during pre-flight.", settings.gemini_fallback_model)
        except Exception as exc:
            logger.warning("Fallback model pre-flight check error: %s", exc)

        return False


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


async def daily_report(
    bot: Any | None = None,
    dry_run: bool = False,
) -> JobLog:
    """Daily report generation and Telegram publishing."""
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
            # Phase R-4: the same bulletin is also delivered as a styled PDF.
            # A PDF failure must never undo an already delivered text report.
            try:
                pdf_info = await publish_daily_report_pdf(
                    report_date=rep_date,
                    items=items,
                    metadata=(db_report.content or {}).get("metadata") if db_report else None,
                    content_quality=content_quality,
                    bot=bot,
                    dry_run=dry_run,
                )
                logger.info("Daily bulletin PDF delivery finished: %s", pdf_info)
            except Exception:
                logger.exception(
                    "Daily bulletin PDF delivery failed; the text report stays published."
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
        daily_report,
        trigger=CronTrigger(
            hour=settings.daily_report_hour,
            minute=settings.daily_report_minute,
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
    return scheduler


# Backward-compatible function aliases
hourly_job = hourly_collection
daily_job = daily_report
