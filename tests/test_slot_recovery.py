"""Slot recovery: due slot, missed slots, and no double processing."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

import pytest

from app.config import settings
from app.reports.daily_report import (
    current_due_slot,
    slot_candidates,
    slot_start,
)
from app.scheduler.jobs import recover_missed_briefings, run_scheduled_briefings


def _tz() -> ZoneInfo:
    return ZoneInfo(settings.timezone)


def test_due_slot_is_the_one_inside_its_grace_window():
    assert current_due_slot(datetime(2026, 11, 14, 8, 0, tzinfo=_tz())) == (date(2026, 11, 14), 8)
    assert current_due_slot(datetime(2026, 11, 14, 9, 30, tzinfo=_tz())) == (date(2026, 11, 14), 8)
    assert current_due_slot(datetime(2026, 11, 14, 20, 1, tzinfo=_tz())) == (date(2026, 11, 14), 20)


def test_no_due_slot_outside_every_grace_window():
    # 14:05 is six hours after the 08:00 start, so nothing is due; recovery owns it now.
    assert current_due_slot(datetime(2026, 11, 14, 14, 5, tzinfo=_tz())) is None


def test_late_run_after_midnight_belongs_to_yesterdays_evening_slot():
    assert current_due_slot(datetime(2026, 11, 15, 0, 42, tzinfo=_tz())) == (date(2026, 11, 14), 20)


def test_recovery_candidates_are_ordered_and_never_in_the_future():
    now = datetime(2026, 11, 14, 9, 0, tzinfo=_tz())
    candidates = slot_candidates(now, lookback_hours=24)

    assert candidates
    assert candidates == sorted(candidates, key=lambda candidate: slot_start(*candidate))
    assert all(slot_start(*candidate) <= now for candidate in candidates)
    assert (date(2026, 11, 14), 8) in candidates
    assert (date(2026, 11, 13), 20) in candidates
    assert (date(2026, 11, 15), 8) not in candidates


def test_recovery_lookback_excludes_older_slots():
    now = datetime(2026, 11, 14, 9, 0, tzinfo=_tz())
    candidates = slot_candidates(now, lookback_hours=6)
    assert (date(2026, 11, 14), 8) in candidates
    assert (date(2026, 11, 13), 20) not in candidates


class _CM:
    def __init__(self):
        self.session = SimpleNamespace()

    async def __aenter__(self):
        return self.session

    async def __aexit__(self, *args):
        return False


@pytest.mark.asyncio
async def test_recovery_publishes_only_slots_without_a_delivery_record(monkeypatch):
    states = {
        (date(2026, 11, 13), 20): {"status": "published"},
        (date(2026, 11, 14), 8): {"status": "in_flight"},
        (date(2026, 11, 14), 20): None,
    }

    async def _slot_status(session, report_date, slot_hour):
        return states.get((report_date, slot_hour))

    monkeypatch.setattr("app.scheduler.jobs.SessionLocal", lambda: _CM())
    monkeypatch.setattr("app.scheduler.jobs.slot_status", _slot_status)
    monkeypatch.setattr(
        "app.scheduler.jobs.slot_candidates",
        lambda now=None: [
            (date(2026, 11, 13), 20),
            (date(2026, 11, 14), 8),
            (date(2026, 11, 14), 20),
        ],
    )

    with patch("app.scheduler.jobs.daily_report", new_callable=AsyncMock) as daily:
        daily.return_value = SimpleNamespace(status="success", error_message=None)
        recovered = await recover_missed_briefings(
            now=datetime(2026, 11, 14, 21, 0, tzinfo=_tz())
        )

    assert [(entry["report_date"], entry["slot_hour"]) for entry in recovered] == [
        (date(2026, 11, 14), 20)
    ]
    daily.assert_awaited_once()
    assert daily.await_args.kwargs["slot_hour"] == 20
    assert daily.await_args.kwargs["report_date"] == date(2026, 11, 14)


@pytest.mark.asyncio
async def test_recovery_never_touches_the_slot_the_current_run_is_publishing(monkeypatch):
    monkeypatch.setattr("app.scheduler.jobs.SessionLocal", lambda: _CM())
    monkeypatch.setattr("app.scheduler.jobs.slot_status", AsyncMock(return_value=None))
    monkeypatch.setattr(
        "app.scheduler.jobs.slot_candidates",
        lambda now=None: [(date(2026, 11, 14), 8), (date(2026, 11, 14), 20)],
    )

    with patch("app.scheduler.jobs.daily_report", new_callable=AsyncMock) as daily:
        daily.return_value = SimpleNamespace(status="success", error_message=None)
        recovered = await recover_missed_briefings(
            now=datetime(2026, 11, 14, 20, 5, tzinfo=_tz()),
            exclude={(date(2026, 11, 14), 20)},
        )

    assert [(entry["report_date"], entry["slot_hour"]) for entry in recovered] == [
        (date(2026, 11, 14), 8)
    ]


@pytest.mark.asyncio
async def test_recovery_budget_bounds_one_run(monkeypatch):
    monkeypatch.setattr(settings, "max_recovery_slots_per_run", 1)
    monkeypatch.setattr("app.scheduler.jobs.SessionLocal", lambda: _CM())
    monkeypatch.setattr("app.scheduler.jobs.slot_status", AsyncMock(return_value=None))
    monkeypatch.setattr(
        "app.scheduler.jobs.slot_candidates",
        lambda now=None: [
            (date(2026, 11, 13), 20),
            (date(2026, 11, 14), 8),
            (date(2026, 11, 14), 20),
        ],
    )

    with patch("app.scheduler.jobs.daily_report", new_callable=AsyncMock) as daily:
        daily.return_value = SimpleNamespace(status="success", error_message=None)
        recovered = await recover_missed_briefings(
            now=datetime(2026, 11, 14, 21, 0, tzinfo=_tz())
        )

    assert len(recovered) == 1
    assert daily.await_count == 1


@pytest.mark.asyncio
async def test_scheduled_run_publishes_the_due_slot_and_excludes_it_from_recovery(monkeypatch):
    now = datetime(2026, 11, 14, 8, 2, tzinfo=_tz())
    monkeypatch.setattr("app.scheduler.jobs.current_due_slot", lambda moment: (date(2026, 11, 14), 8))

    with patch("app.scheduler.jobs.daily_report", new_callable=AsyncMock) as daily, \
         patch("app.scheduler.jobs.recover_missed_briefings", new_callable=AsyncMock) as recovery:
        daily.return_value = SimpleNamespace(status="success", error_message=None)
        recovery.return_value = []
        logs = await run_scheduled_briefings(now=now)

    assert len(logs) == 1
    daily.assert_awaited_once()
    assert daily.await_args.kwargs["slot_hour"] == 8
    assert daily.await_args.kwargs["report_date"] == date(2026, 11, 14)
    assert recovery.await_args.kwargs["exclude"] == {(date(2026, 11, 14), 8)}
    assert recovery.await_args.kwargs["now"] == now


@pytest.mark.asyncio
async def test_a_slot_is_not_treated_as_missed_before_it_starts():
    now = datetime(2026, 11, 14, 7, 55, tzinfo=_tz())
    candidates = slot_candidates(now, lookback_hours=24)
    assert (date(2026, 11, 14), 8) not in candidates
    assert current_due_slot(now) is None
    assert timedelta(0) <= now - slot_start(date(2026, 11, 13), 20)
