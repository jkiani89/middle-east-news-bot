"""Tests for scripts/run_once.py batch runner."""

from __future__ import annotations

from datetime import datetime
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

import pytest

from app.config import settings
from app.database.models import DailyReport
from scripts.run_once import run_batch_pipeline, should_run_daily_report


@pytest.fixture(autouse=True)
def slot_table(monkeypatch):
    """Empty bulletin_slots table by default; tests that need a row seed this dict."""
    rows: dict = {}

    async def _slot_status(session, report_date, slot_hour):
        return rows.get((report_date, slot_hour))

    monkeypatch.setattr("scripts.run_once.slot_status", _slot_status)
    return rows


@pytest.mark.asyncio
async def test_should_run_daily_report_already_published():
    """If daily report for today is already marked published with message IDs, skip."""
    tz = ZoneInfo("Asia/Tehran")
    today = datetime.now(tz).date()

    mock_rep = DailyReport(
        report_date=today,
        status="published",
        telegram_message_ids=[101, 102],
    )
    with patch("scripts.run_once.SessionLocal") as mock_session_local:
        mock_session = AsyncMock()
        mock_session.scalar.return_value = mock_rep
        mock_session_local.return_value.__aenter__.return_value = mock_session

        should_run, reason = await should_run_daily_report()
        assert should_run is False
        assert "already published" in reason


@pytest.mark.asyncio
async def test_should_run_daily_report_forced():
    """Forced flag returns True regardless of window."""
    with patch("scripts.run_once.SessionLocal") as mock_session_local:
        mock_session = AsyncMock()
        mock_session.scalar.return_value = None
        mock_session_local.return_value.__aenter__.return_value = mock_session

        should_run, reason = await should_run_daily_report(force=True, target_hour=99)
        assert should_run is True
        assert "Forced" in reason


@pytest.mark.asyncio
async def test_should_run_daily_report_window_matching():
    """If current hour in Tehran matches target_hour, return True."""
    tz = ZoneInfo("Asia/Tehran")
    current_hour = datetime.now(tz).hour

    with patch("scripts.run_once.SessionLocal") as mock_session_local:
        mock_session = AsyncMock()
        mock_session.scalar.return_value = None
        mock_session_local.return_value.__aenter__.return_value = mock_session

        should_run, reason = await should_run_daily_report(target_hour=current_hour)
        assert should_run is True
        assert "matches" in reason


@pytest.mark.asyncio
async def test_should_run_daily_report_outside_window():
    """If current hour does not match target_hour, return False."""
    tz = ZoneInfo("Asia/Tehran")
    current_hour = datetime.now(tz).hour
    other_hour = (current_hour + 5) % 24

    with patch("scripts.run_once.SessionLocal") as mock_session_local:
        mock_session = AsyncMock()
        mock_session.scalar.return_value = None
        mock_session_local.return_value.__aenter__.return_value = mock_session

        should_run, reason = await should_run_daily_report(target_hour=other_hour)
        assert should_run is False
        assert "outside" in reason


@pytest.mark.asyncio
async def test_should_run_daily_report_inside_catch_up_window():
    """A run that starts late (09:30 for the 08:00 slot) still publishes that slot."""
    tz = ZoneInfo("Asia/Tehran")
    late_run = datetime(2026, 11, 14, 9, 30, tzinfo=tz)

    with (
        patch("scripts.run_once.local_now", return_value=late_run),
        patch("scripts.run_once.SessionLocal") as mock_session_local,
    ):
        mock_session = AsyncMock()
        mock_session.scalar.return_value = None
        mock_session_local.return_value.__aenter__.return_value = mock_session

        should_run, reason = await should_run_daily_report(slot_hour=8)
        assert should_run is True
        assert "matches" in reason


@pytest.mark.asyncio
async def test_should_run_daily_report_after_catch_up_window():
    """Three hours after the slot the latch closes: 11:05 for the 08:00 slot is late."""
    tz = ZoneInfo("Asia/Tehran")
    too_late = datetime(2026, 11, 14, 11, 5, tzinfo=tz)

    with (
        patch("scripts.run_once.local_now", return_value=too_late),
        patch("scripts.run_once.SessionLocal") as mock_session_local,
    ):
        mock_session = AsyncMock()
        mock_session.scalar.return_value = None
        mock_session_local.return_value.__aenter__.return_value = mock_session

        should_run, reason = await should_run_daily_report(slot_hour=8)
        assert should_run is False
        assert "outside" in reason


@pytest.mark.asyncio
async def test_should_run_daily_report_evening_slot_window():
    """The 20:00 slot is still publishable at 22:45 but not at 19:59."""
    tz = ZoneInfo("Asia/Tehran")

    for moment, expected in (
        (datetime(2026, 11, 14, 22, 45, tzinfo=tz), True),
        (datetime(2026, 11, 14, 19, 59, tzinfo=tz), False),
    ):
        with (
            patch("scripts.run_once.local_now", return_value=moment),
            patch("scripts.run_once.SessionLocal") as mock_session_local,
        ):
            mock_session = AsyncMock()
            mock_session.scalar.return_value = None
            mock_session_local.return_value.__aenter__.return_value = mock_session

            should_run, _reason = await should_run_daily_report(slot_hour=20)
            assert should_run is expected, moment


@pytest.mark.asyncio
async def test_should_run_daily_report_window_crosses_midnight():
    """With a 5-hour grace the 20:00 slot stays open until 01:00 the next day."""
    tz = ZoneInfo("Asia/Tehran")

    for moment, expected in (
        (datetime(2026, 11, 15, 0, 30, tzinfo=tz), True),
        (datetime(2026, 11, 15, 1, 30, tzinfo=tz), False),
    ):
        with (
            patch("scripts.run_once.local_now", return_value=moment),
            patch.object(settings, "slot_grace_hours", 5),
            patch("scripts.run_once.SessionLocal") as mock_session_local,
        ):
            mock_session = AsyncMock()
            mock_session.scalar.return_value = None
            mock_session_local.return_value.__aenter__.return_value = mock_session

            should_run, reason = await should_run_daily_report(slot_hour=20)
            assert should_run is expected, moment
            if expected is False:
                assert "01:00 (+1d)" in reason


@pytest.mark.asyncio
async def test_should_run_daily_report_skips_a_slot_in_flight(slot_table):
    """A slot claimed by a live run is not started a second time."""
    tz = ZoneInfo("Asia/Tehran")
    claim = datetime.now(tz)
    slot_table[(claim.date(), 8)] = {
        "status": "in_flight",
        "claimed_at": claim,
        "message_ids": [],
    }
    report = DailyReport(report_date=claim.date(), status="generated", content={})

    with (
        patch("scripts.run_once.local_now", return_value=claim),
        patch("scripts.run_once.SessionLocal") as mock_session_local,
    ):
        mock_session = AsyncMock()
        mock_session.scalar.return_value = report
        mock_session_local.return_value.__aenter__.return_value = mock_session

        should_run, reason = await should_run_daily_report(slot_hour=8)
        assert should_run is False
        assert "in flight" in reason


@pytest.mark.asyncio
async def test_run_batch_pipeline_flow():
    """End-to-end mocked run of the batch pipeline."""
    with (
        patch("scripts.run_once.sync_sources", new_callable=AsyncMock) as mock_sync,
        patch("scripts.run_once.reconcile_interrupted_jobs", new_callable=AsyncMock) as mock_recon,
        patch("scripts.run_once.collect_and_process", new_callable=AsyncMock) as mock_collect,
        patch("scripts.run_once.should_run_daily_report", new_callable=AsyncMock) as mock_should,
        patch("scripts.run_once.daily_report", new_callable=AsyncMock) as mock_daily,
        patch("scripts.run_once.cleanup_gemini_request_log", new_callable=AsyncMock) as mock_clean,
    ):
        mock_collect.return_value = 5
        mock_should.return_value = (True, "Testing")
        mock_job = AsyncMock()
        mock_job.status = "success"
        mock_job.processed_count = 3
        mock_daily.return_value = mock_job

        exit_code = await run_batch_pipeline(force_daily_report=True, dry_run_daily_report=True)
        assert exit_code == 0
        mock_sync.assert_awaited_once()
        mock_recon.assert_awaited_once()
        mock_collect.assert_awaited_once()
        mock_daily.assert_awaited_once_with(dry_run=True)
        mock_clean.assert_awaited_once()
