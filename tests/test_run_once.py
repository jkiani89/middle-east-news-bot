"""Tests for scripts/run_once.py batch runner."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

import pytest

from app.config import settings
from app.database.models import DailyReport
from app.reports.daily_report import latest_slot_start, report_window, slot_report_date
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
    """Six hours after the slot the latch closes: 14:05 for the 08:00 slot is late."""
    tz = ZoneInfo("Asia/Tehran")
    too_late = datetime(2026, 11, 14, 14, 5, tzinfo=tz)

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
@pytest.mark.parametrize(
    "moment",
    [
        # Inside the 08:00-16:00 catch-up window.
        datetime(2026, 11, 16, 8, 30, tzinfo=ZoneInfo("Asia/Tehran")),
        datetime(2026, 11, 16, 13, 59, tzinfo=ZoneInfo("Asia/Tehran")),
        # Outside the window: before the slot, after it, and past midnight.
        datetime(2026, 11, 16, 6, 0, tzinfo=ZoneInfo("Asia/Tehran")),
        datetime(2026, 11, 16, 15, 0, tzinfo=ZoneInfo("Asia/Tehran")),
        datetime(2026, 11, 16, 0, 30, tzinfo=ZoneInfo("Asia/Tehran")),
    ],
    ids=["inside-window", "window-edge", "before-slot", "after-window", "after-midnight"],
)
async def test_should_run_daily_report_skips_a_slot_in_flight(slot_table, moment):
    """A slot claimed by a live run is not started a second time.

    The moment is fixed, so the result no longer depends on the wall clock or the CI
    timezone; the in-flight row is seeded for the slot day the run actually resolves.
    """
    slot_date = latest_slot_start(moment, 8).date()
    claimed_at = moment - timedelta(minutes=5)
    slot_table[(slot_date, 8)] = {
        "status": "in_flight",
        "claimed_at": claimed_at,
        "message_ids": [],
    }
    report = DailyReport(report_date=slot_date, status="generated", content={})

    with (
        patch("scripts.run_once.local_now", return_value=moment),
        patch("scripts.run_once.SessionLocal") as mock_session_local,
    ):
        mock_session = AsyncMock()
        mock_session.scalar.return_value = report
        mock_session_local.return_value.__aenter__.return_value = mock_session

        should_run, reason = await should_run_daily_report(slot_hour=8)
        assert should_run is False
        assert "in flight" in reason


# --- Phase Y-10: a late run belongs to the slot's own day -------------------


@pytest.mark.asyncio
async def test_late_evening_run_publishes_yesterdays_slot():
    """00:42 Tehran is 4h42 after yesterday's 20:00 slot, so that slot still ships."""
    tz = ZoneInfo("Asia/Tehran")
    late = datetime(2026, 11, 16, 0, 42, tzinfo=tz)

    with (
        patch("scripts.run_once.local_now", return_value=late),
        patch("scripts.run_once.SessionLocal") as mock_session_local,
    ):
        mock_session = AsyncMock()
        mock_session.scalar.return_value = None
        mock_session_local.return_value.__aenter__.return_value = mock_session

        should_run, reason = await should_run_daily_report(slot_hour=20)

    assert should_run is True
    assert "matches" in reason
    # The bulletin, its window and its claim key all belong to 2026-11-15.
    assert slot_report_date(20, late) == date(2026, 11, 15)
    _day, start, end = report_window(slot_report_date(20, late), 20)
    assert end == datetime(2026, 11, 15, 20, 0, tzinfo=tz)
    assert start == datetime(2026, 11, 15, 8, 0, tzinfo=tz)


@pytest.mark.asyncio
async def test_same_late_run_is_outside_a_three_hour_grace():
    """With grace=3 the 00:42 run is too late for yesterday's 20:00 slot."""
    tz = ZoneInfo("Asia/Tehran")
    late = datetime(2026, 11, 16, 0, 42, tzinfo=tz)

    with (
        patch("scripts.run_once.local_now", return_value=late),
        patch.object(settings, "slot_grace_hours", 3),
        patch("scripts.run_once.SessionLocal") as mock_session_local,
    ):
        mock_session = AsyncMock()
        mock_session.scalar.return_value = None
        mock_session_local.return_value.__aenter__.return_value = mock_session

        should_run, reason = await should_run_daily_report(slot_hour=20)

    assert should_run is False
    assert "outside" in reason


@pytest.mark.asyncio
async def test_morning_slot_still_uses_today():
    tz = ZoneInfo("Asia/Tehran")
    morning = datetime(2026, 11, 14, 9, 30, tzinfo=tz)

    with (
        patch("scripts.run_once.local_now", return_value=morning),
        patch("scripts.run_once.SessionLocal") as mock_session_local,
    ):
        mock_session = AsyncMock()
        mock_session.scalar.return_value = None
        mock_session_local.return_value.__aenter__.return_value = mock_session

        should_run, _reason = await should_run_daily_report(slot_hour=8)

    assert should_run is True
    assert slot_report_date(8, morning) == date(2026, 11, 14)


@pytest.mark.asyncio
async def test_previous_day_published_slot_is_not_claimed_again(slot_table):
    """Reproduces the bug: the dedup key must be yesterday's slot, not today's."""
    tz = ZoneInfo("Asia/Tehran")
    late = datetime(2026, 11, 16, 0, 42, tzinfo=tz)
    slot_table[(date(2026, 11, 15), 20)] = {
        "status": "published",
        "claimed_at": None,
        "message_ids": [7],
    }

    with (
        patch("scripts.run_once.local_now", return_value=late),
        patch("scripts.run_once.SessionLocal") as mock_session_local,
    ):
        mock_session = AsyncMock()
        mock_session.scalar.return_value = None
        mock_session_local.return_value.__aenter__.return_value = mock_session

        should_run, reason = await should_run_daily_report(slot_hour=20)

    assert should_run is False
    assert "already published" in reason


def test_evening_slot_belongs_to_its_own_day_on_consecutive_days():
    tz = ZoneInfo("Asia/Tehran")
    assert slot_report_date(20, datetime(2026, 11, 16, 0, 42, tzinfo=tz)) == date(2026, 11, 15)
    assert slot_report_date(20, datetime(2026, 11, 17, 0, 42, tzinfo=tz)) == date(2026, 11, 16)


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
        patch("scripts.run_once.engine", new_callable=AsyncMock) as mock_engine,
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
        mock_engine.dispose.assert_awaited_once()


def test_main_hard_exits_instead_of_joining_lingering_workers(monkeypatch):
    """asyncio.run()'s 300-second executor join must never delay the workflow exit.

    ``asyncio.run`` calls ``Runner.close()`` -> ``shutdown_default_executor(300)``; a
    google-genai call that outlived its ``wait_for`` keeps such a worker busy, so the
    process used to hang after "Step 4/4". main() now drives the loop itself and leaves
    with ``os._exit`` as soon as a non-daemon thread is still alive.
    """
    import scripts.run_once as run_once_module

    async def _fake_pipeline(**_kwargs):
        return 0

    exited: list[int] = []

    def _fake_os_exit(code: int = 0) -> None:
        exited.append(code)
        raise SystemExit(code)

    monkeypatch.setattr(run_once_module, "run_batch_pipeline", _fake_pipeline)
    monkeypatch.setattr(run_once_module, "_lingering_non_daemon_threads", lambda: [object()])
    monkeypatch.setattr(run_once_module, "flush_gemini_audit", lambda *args, **kwargs: 0)
    monkeypatch.setattr(run_once_module.logging, "shutdown", lambda: None)
    monkeypatch.setattr(run_once_module.os, "_exit", _fake_os_exit)
    monkeypatch.setattr(run_once_module.sys, "argv", ["run_once.py"])
    monkeypatch.setattr(settings, "immediate_publish_dry_run", settings.immediate_publish_dry_run)

    with pytest.raises(SystemExit) as excinfo:
        run_once_module.main()

    assert exited == [0]
    assert excinfo.value.code == 0
