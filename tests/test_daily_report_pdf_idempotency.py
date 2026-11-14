"""Phase S: the PDF bulletin is delivered at most once per report.

A partial daily-report delivery is resumed from ``daily_report.telegram_message_ids``;
the PDF message id now travels with it, so a resumed run never posts the same
document twice.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.scheduler.jobs import daily_report


class _FakeSession:
    def __init__(self, report: SimpleNamespace | None):
        self.report = report
        self.added: list[object] = []
        self.commits = 0

    def add(self, obj: object) -> None:
        self.added.append(obj)

    async def commit(self) -> None:
        self.commits += 1

    async def refresh(self, obj: object) -> None:
        if not getattr(obj, "id", None):
            obj.id = 1

    async def scalar(self, stmt: object) -> object:
        return self.report

    async def execute(self, stmt: object) -> object:
        result = MagicMock()
        result.rowcount = 1
        return result


class _FakeSessionLocal:
    def __init__(self, session: _FakeSession):
        self.session = session

    async def __aenter__(self) -> _FakeSession:
        return self.session

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        pass


def _report(content: dict, message_ids: list[int]) -> SimpleNamespace:
    return SimpleNamespace(
        id=7,
        report_date=datetime.now(UTC).date(),
        content=content,
        telegram_message_ids=message_ids,
        status="generated",
        published_at=None,
        generated_at=None,
    )


def _patch_session(session: _FakeSession):
    return patch("app.scheduler.jobs.SessionLocal", return_value=_FakeSessionLocal(session))


@pytest.mark.asyncio
async def test_pdf_message_id_is_recorded_after_delivery():
    report = _report({"content_quality": "real"}, [11, 12])
    session = _FakeSession(report)

    with _patch_session(session), \
         patch("app.scheduler.jobs.generate_and_save_daily_report", new_callable=AsyncMock) as gen, \
         patch("app.scheduler.jobs.publish_report", new_callable=AsyncMock) as publish, \
         patch("app.scheduler.jobs.publish_daily_report_pdf", new_callable=AsyncMock) as publish_pdf:
        gen.return_value = (datetime.now(UTC).date(), [{"event_id": 1}], "text")
        publish.return_value = [{"message_id": 11}, {"message_id": 12}]
        publish_pdf.return_value = {"message_id": 67, "type": "document", "bytes": 1000}

        job_log = await daily_report(dry_run=False)

    assert job_log.status == "success"
    assert publish_pdf.await_count == 1
    assert report.content["pdf_message_id"] == 67
    assert report.telegram_message_ids == [11, 12, 67]
    assert session.commits >= 1


@pytest.mark.asyncio
async def test_resumed_run_does_not_resend_the_pdf():
    report = _report({"content_quality": "real", "pdf_message_id": 67}, [11, 12, 67])
    session = _FakeSession(report)

    with _patch_session(session), \
         patch("app.scheduler.jobs.generate_and_save_daily_report", new_callable=AsyncMock) as gen, \
         patch("app.scheduler.jobs.publish_report", new_callable=AsyncMock) as publish, \
         patch("app.scheduler.jobs.publish_daily_report_pdf", new_callable=AsyncMock) as publish_pdf:
        gen.return_value = (datetime.now(UTC).date(), [{"event_id": 1}], "text")
        publish.return_value = [{"message_id": 11}, {"message_id": 12}]

        job_log = await daily_report(dry_run=False)

    assert job_log.status == "success"
    assert publish_pdf.await_count == 0
    assert report.telegram_message_ids == [11, 12, 67]


@pytest.mark.asyncio
async def test_pdf_failure_does_not_fail_the_report():
    report = _report({"content_quality": "real"}, [11])
    session = _FakeSession(report)

    with _patch_session(session), \
         patch("app.scheduler.jobs.generate_and_save_daily_report", new_callable=AsyncMock) as gen, \
         patch("app.scheduler.jobs.publish_report", new_callable=AsyncMock) as publish, \
         patch("app.scheduler.jobs.publish_daily_report_pdf", new_callable=AsyncMock) as publish_pdf:
        gen.return_value = (datetime.now(UTC).date(), [{"event_id": 1}], "text")
        publish.return_value = [{"message_id": 11}]
        publish_pdf.side_effect = RuntimeError("telegram is down")

        job_log = await daily_report(dry_run=False)

    assert job_log.status == "success"
    assert "pdf_message_id" not in report.content
    assert report.telegram_message_ids == [11]


@pytest.mark.asyncio
async def test_dry_run_never_records_a_message_id():
    report = _report({"content_quality": "real"}, [])
    session = _FakeSession(report)

    with _patch_session(session), \
         patch("app.scheduler.jobs.generate_and_save_daily_report", new_callable=AsyncMock) as gen, \
         patch("app.scheduler.jobs.publish_report", new_callable=AsyncMock) as publish, \
         patch("app.scheduler.jobs.publish_daily_report_pdf", new_callable=AsyncMock) as publish_pdf:
        gen.return_value = (datetime.now(UTC).date(), [{"event_id": 1}], "text")
        publish.return_value = [{"message_id": 0}]
        publish_pdf.return_value = {"message_id": 0, "type": "document", "dry_run": True}

        job_log = await daily_report(dry_run=True)

    assert job_log.status == "success"
    assert "pdf_message_id" not in report.content
