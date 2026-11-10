"""Scheduler jobs and lifecycle management for Middle East News Bot."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import logging
from typing import Any, Callable, Coroutine
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy import select, update

from app.ai.provider import (
    AIProviderError,
    AIRateLimitError,
    AIServiceUnavailableError,
    BaseAIProvider,
    GeminiProvider,
)
from app.config import settings
from app.database.connection import SessionLocal
from app.database.models import DailyReport, JobLog
from app.processors.pipeline import collect_and_process
from app.reports.daily_report import generate_and_save_daily_report
from app.telegram.publisher import MockPublishRefusedError, publish_report

logger = logging.getLogger(__name__)


def utcnow() -> datetime:
    """Return timezone-aware current UTC datetime."""
    return datetime.now(timezone.utc)


async def check_primary_model_available(provider: BaseAIProvider | None = None) -> bool:
    """Quick health check before hourly run to avoid mass-fallback to lower-quality model."""
    if provider is None:
        provider = GeminiProvider(
            primary_model=settings.gemini_primary_model,
            enable_fallback=False,  # Test only the primary model
        )
    try:
        res = await provider.generate_json('Respond with {"ok": true}')
        return bool(res and res.get("ok") is True)
    except (AIRateLimitError, AIServiceUnavailableError):
        return False
    except Exception as exc:
        logger.warning("Primary model pre-flight check encountered error: %s", exc)
        return False


async def hourly_collection(
    provider: BaseAIProvider | None = None,
    health_checker: Callable[[], Coroutine[Any, Any, bool]] | None = None,
) -> JobLog:
    """Hourly: collect -> process -> event match. Does NOT publish."""
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
            # Step 1: Pre-flight check
            is_healthy: bool = False
            if health_checker is not None:
                is_healthy = await health_checker()
            else:
                is_healthy = await check_primary_model_available(provider=provider)

            if not is_healthy:
                logger.warning("Primary model unavailable during pre-flight check. Skipping this hourly cycle.")
                job_log.status = "skipped_primary_unavailable"
                return job_log

            # Step 2: Ingestion & processing
            processed_count = await collect_and_process(provider=provider)
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
    return scheduler


# Backward-compatible function aliases
hourly_job = hourly_collection
daily_job = daily_report
