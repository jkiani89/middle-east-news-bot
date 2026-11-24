from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from app.notifications import alerts


@pytest.mark.asyncio
async def test_alert_triggers_on_failed_status():
    send = AsyncMock()
    sent = await alerts.maybe_send_admin_alert(
        status="failed",
        collected_count=10,
        duration_seconds=100,
        recent_alert_exists=False,
        send_func=send,
    )
    assert sent == ["failed"]
    assert send.await_count == 1
    assert "Pipeline alert: failed" in send.await_args.args[0]


@pytest.mark.asyncio
async def test_alert_triggers_on_queue_backlog_and_slow_run():
    send = AsyncMock()
    sent = await alerts.maybe_send_admin_alert(
        status="success",
        collected_count=95,
        duration_seconds=1600,
        recent_alert_exists=False,
        send_func=send,
    )
    assert "queue_backlog" in sent
    assert "slow_run" in sent
    assert send.await_count == 2


@pytest.mark.asyncio
async def test_cooldown_suppresses_duplicate_alert():
    send = AsyncMock()
    sent = await alerts.maybe_send_admin_alert(
        status="failed",
        collected_count=10,
        duration_seconds=100,
        recent_alert_exists=True,
        send_func=send,
    )
    assert sent == []
    send.assert_not_awaited()


@pytest.mark.asyncio
async def test_telegram_failure_does_not_raise():
    send = AsyncMock(side_effect=RuntimeError("telegram down"))
    sent = await alerts.maybe_send_admin_alert(
        status="stopped_unavailable",
        collected_count=10,
        duration_seconds=100,
        recent_alert_exists=False,
        send_func=send,
    )
    assert sent == []
    # Function must return normally (no exception propagated).


@pytest.mark.asyncio
async def test_telegram_timeout_is_bounded():
    async def slow_send(text):
        await asyncio.sleep(10)

    sent = await alerts.maybe_send_admin_alert(
        status="failed",
        collected_count=10,
        duration_seconds=100,
        recent_alert_exists=False,
        send_func=slow_send,
    )
    # With a 5s ceiling the slow send is cancelled and recorded as failed.
    assert sent == []


def test_classify_alerts_none_when_healthy():
    assert alerts.classify_alerts("success", 10, 100) == []


def test_format_admin_alert_contains_tehran_time():
    text = alerts.format_admin_alert("failed", 42, 120, now=__import__("datetime").datetime(2026, 1, 1, tzinfo=__import__("datetime").timezone.utc))
    assert "Collected queue: 42" in text
    assert "Time (Tehran):" in text
