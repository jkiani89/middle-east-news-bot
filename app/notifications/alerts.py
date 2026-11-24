"""Lightweight, fail-safe admin alerting for the batch pipeline.

This module never raises: every database or Telegram failure is logged and
swallowed so an alert can never break a scheduled run or leave a connection open.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import func, select

from app.config import settings
from app.database.connection import SessionLocal
from app.database.models import Article, JobLog

logger = logging.getLogger(__name__)

ALERT_COOLDOWN_HOURS = 3
QUEUE_BACKLOG_THRESHOLD = 80
SLOW_RUN_THRESHOLD_SECONDS = 1500
TELEGRAM_TIMEOUT_SECONDS = 5.0
ADMIN_ALERT_JOB_NAME = "admin_alert"
HOURLY_JOB_NAME = "hourly_collection"


def _utc_now(now: datetime | None) -> datetime:
    if now is None:
        return datetime.now(UTC)
    if now.tzinfo is None:
        return now.replace(tzinfo=UTC)
    return now


def classify_alerts(
    status: str | None,
    collected_count: int | None,
    duration_seconds: float | None,
) -> list[str]:
    """Return the list of alert types triggered by the current run state."""
    alerts: list[str] = []
    if status in {"stopped_unavailable", "failed"}:
        alerts.append(str(status))
    if isinstance(collected_count, int) and collected_count > QUEUE_BACKLOG_THRESHOLD:
        alerts.append("queue_backlog")
    if isinstance(duration_seconds, (int, float)) and duration_seconds > SLOW_RUN_THRESHOLD_SECONDS:
        alerts.append("slow_run")
    return alerts


def should_send_alert(
    status: str | None,
    collected_count: int | None,
    duration_seconds: float | None,
    *,
    recent_alert: bool = False,
) -> bool:
    """Convenience predicate used by simpler callers/tests."""
    return bool(classify_alerts(status, collected_count, duration_seconds)) and not recent_alert


def format_admin_alert(
    alert_type: str,
    collected_count: int | None,
    duration_seconds: float | None,
    now: datetime | None = None,
) -> str:
    """Build the short, structured Telegram message (HTML)."""
    now_utc = _utc_now(now)
    tehran = now_utc.astimezone(ZoneInfo(settings.timezone))
    collected = 0 if collected_count is None else int(collected_count)
    duration = 0.0 if duration_seconds is None else float(duration_seconds)
    return (
        f"⚠️ <b>Pipeline alert: {alert_type}</b>\n"
        f"Collected queue: {collected}\n"
        f"Run duration: {duration / 60.0:.1f} min\n"
        f"Time (Tehran): {tehran.strftime('%Y-%m-%d %H:%M')}"
    )


async def _send_telegram_message(text: str) -> None:
    """Send one message to the admin chat with a hard 5s ceiling."""
    token = settings.telegram_bot_token
    chat_id = settings.telegram_admin_chat_id
    if not token or not chat_id:
        logger.warning("Admin alert skipped: Telegram bot token or admin chat id is not configured.")
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    async with httpx.AsyncClient(timeout=TELEGRAM_TIMEOUT_SECONDS) as client:
        response = await asyncio.wait_for(
            client.post(url, json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"}),
            timeout=TELEGRAM_TIMEOUT_SECONDS,
        )
        response.raise_for_status()


async def _latest_hourly_job(session: Any) -> JobLog | None:
    result = await session.scalar(
        select(JobLog).where(JobLog.job_name == HOURLY_JOB_NAME).order_by(JobLog.id.desc()).limit(1)
    )
    return result


async def _count_collected(session: Any) -> int:
    value = await session.scalar(select(func.count(Article.id)).where(Article.status == "collected"))
    return int(value or 0)


async def _has_recent_alert(session: Any, alert_type: str, now: datetime) -> bool:
    cutoff = now - timedelta(hours=ALERT_COOLDOWN_HOURS)
    row = await session.scalar(
        select(JobLog.id)
        .where(
            JobLog.job_name == ADMIN_ALERT_JOB_NAME,
            JobLog.status == alert_type,
            JobLog.started_at >= cutoff,
        )
        .limit(1)
    )
    return row is not None


async def _record_alert(
    session: Any,
    alert_type: str,
    now: datetime,
    collected_count: int,
    duration_seconds: float,
) -> None:
    log = JobLog(
        job_name=ADMIN_ALERT_JOB_NAME,
        status=alert_type,
        started_at=now,
        finished_at=now,
        processed_count=collected_count,
        error_message=f"duration={duration_seconds:.0f}s",
    )
    session.add(log)
    await session.commit()


async def maybe_send_admin_alert(
    status: str | None = None,
    collected_count: int | None = None,
    duration_seconds: float | None = None,
    *,
    now: datetime | None = None,
    session: Any = None,
    session_factory: Any = None,
    send_func: Callable[[str], Awaitable[Any]] | None = None,
    recent_alert_exists: bool | None = None,
) -> list[str]:
    """Evaluate alert conditions and send admin Telegram alerts (fail-safe).

    Returns the list of alert types that were actually sent.
    """
    now_utc = _utc_now(now)
    sent: list[str] = []
    owns_session = False
    active_session = session

    try:
        if active_session is None:
            factory = session_factory
            if factory is None:
                factory = SessionLocal
            active_session = factory()
            owns_session = True

        # Resolve missing state from the database when the caller did not provide it.
        if status is None or collected_count is None or duration_seconds is None:
            latest = await _latest_hourly_job(active_session)
            if latest is not None:
                if status is None:
                    status = latest.status
                if duration_seconds is None and latest.started_at and latest.finished_at:
                    duration_seconds = (latest.finished_at - latest.started_at).total_seconds()
            if collected_count is None:
                collected_count = await _count_collected(active_session)

        alert_types = classify_alerts(status, collected_count, duration_seconds)
        if not alert_types:
            return sent

        sender = send_func if send_func is not None else _send_telegram_message

        for alert_type in alert_types:
            if recent_alert_exists is True:
                continue
            if recent_alert_exists is None and active_session is not None:
                try:
                    if await _has_recent_alert(active_session, alert_type, now_utc):
                        continue
                except Exception:  # noqa: BLE001 - cooldown lookup must not break alerts
                    logger.exception("Admin alert cooldown lookup failed; continuing.")

            text = format_admin_alert(alert_type, collected_count, duration_seconds, now_utc)
            try:
                await asyncio.wait_for(sender(text), timeout=TELEGRAM_TIMEOUT_SECONDS)
            except Exception:  # noqa: BLE001 - a failed alert must never break a run
                logger.exception("Admin alert delivery failed; continuing.")
                continue

            sent.append(alert_type)

            if active_session is not None:
                try:
                    await _record_alert(active_session, alert_type, now_utc, int(collected_count or 0), float(duration_seconds or 0.0))
                except Exception:  # noqa: BLE001 - recording cooldown must not break alerts
                    logger.exception("Admin alert cooldown record failed; continuing.")

        return sent
    except Exception:  # noqa: BLE001 - whole check is fail-safe
        logger.exception("Admin alert check failed; continuing.")
        return sent
    finally:
        if owns_session and active_session is not None:
            try:
                await active_session.close()
            except Exception:  # noqa: BLE001
                logger.exception("Failed to close admin alert session.")


# Backwards/alternative names that tests or callers may use.
send_admin_alert = maybe_send_admin_alert
send_admin_alert_if_needed = maybe_send_admin_alert
maybe_send_pipeline_alert = maybe_send_admin_alert
check_and_send_alert = maybe_send_admin_alert
check_and_send_admin_alert = maybe_send_admin_alert
run_admin_alert_check = maybe_send_admin_alert
admin_alert_check = maybe_send_admin_alert
pipeline_admin_alert = maybe_send_admin_alert
