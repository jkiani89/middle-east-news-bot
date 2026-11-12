"""Unit and contract tests for Phase 8 Scheduler, JobLog tracking, and error resilience."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.ai.provider import (
    AIProviderError,
    AIRateLimitError,
    AIServiceUnavailableError,
    MockAIProvider,
)
from app.config import settings
from app.scheduler.jobs import (
    build_scheduler,
    check_any_model_available,
    daily_report,
    hourly_collection,
    reconcile_interrupted_jobs,
)
from app.telegram.publisher import MockPublishRefusedError


class InMemoryMockSession:
    """Mock async SQLAlchemy session for unit and contract testing."""

    def __init__(self):
        self.added: list[Any] = []
        self.committed: bool = False
        self._next_id = 100

    def add(self, obj: Any) -> None:
        if not getattr(obj, "id", None):
            obj.id = self._next_id
            self._next_id += 1
        self.added.append(obj)

    async def commit(self) -> None:
        self.committed = True

    async def refresh(self, obj: Any) -> None:
        if not getattr(obj, "id", None):
            obj.id = self._next_id
            self._next_id += 1

    async def scalar(self, stmt: Any) -> Any:
        return 0

    async def execute(self, stmt: Any) -> Any:
        mock_result = MagicMock()
        mock_result.rowcount = 1
        return mock_result


class MockSessionContextManager:
    def __init__(self, session: InMemoryMockSession | None = None):
        self.session = session or InMemoryMockSession()

    async def __aenter__(self) -> InMemoryMockSession:
        return self.session

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        pass


@pytest.fixture
def mock_session_local():
    """Fixture providing a mock SessionLocal context manager."""
    session = InMemoryMockSession()
    cm = MockSessionContextManager(session)
    with patch("app.scheduler.jobs.SessionLocal", return_value=cm):
        yield session


@pytest.mark.asyncio
async def test_hourly_job_records_failed_quota_on_AIRateLimitError(mock_session_local):
    """Contract Test 1: Hourly collection records 'failed_quota' on AIRateLimitError."""
    with patch("app.scheduler.jobs.collect_new_articles", new_callable=AsyncMock), \
         patch("app.scheduler.jobs.process_collected_articles", new_callable=AsyncMock) as mock_pipeline:
        mock_pipeline.side_effect = AIRateLimitError("429 Resource Exhausted")

        job_log = await hourly_collection(
            health_checker=AsyncMock(return_value=True),
            pending_checker=AsyncMock(return_value=1),
        )

        assert job_log.status == "failed_quota"
        assert "429 Resource Exhausted" in (job_log.error_message or "")
        assert job_log.finished_at is not None
        assert mock_session_local.committed is True


@pytest.mark.asyncio
async def test_hourly_job_records_failed_unavailable_on_AIServiceUnavailableError(mock_session_local):
    """Contract Test 2: Hourly collection records 'failed_unavailable' on AIServiceUnavailableError."""
    with patch("app.scheduler.jobs.collect_new_articles", new_callable=AsyncMock), \
         patch("app.scheduler.jobs.process_collected_articles", new_callable=AsyncMock) as mock_pipeline:
        mock_pipeline.side_effect = AIServiceUnavailableError("503 Service Unavailable: High Demand")

        job_log = await hourly_collection(
            health_checker=AsyncMock(return_value=True),
            pending_checker=AsyncMock(return_value=1),
        )

        assert job_log.status == "failed_unavailable"
        assert "503 Service Unavailable" in (job_log.error_message or "")
        assert job_log.finished_at is not None
        assert mock_session_local.committed is True


@pytest.mark.asyncio
async def test_hourly_job_records_failed_ai_on_AIProviderError(mock_session_local):
    """Contract Test 3: Hourly collection records 'failed_ai' on unrecoverable AIProviderError."""
    with patch("app.scheduler.jobs.collect_new_articles", new_callable=AsyncMock), \
         patch("app.scheduler.jobs.process_collected_articles", new_callable=AsyncMock) as mock_pipeline:
        mock_pipeline.side_effect = AIProviderError("Invalid JSON structure returned by Gemini")

        job_log = await hourly_collection(
            health_checker=AsyncMock(return_value=True),
            pending_checker=AsyncMock(return_value=1),
        )

        assert job_log.status == "failed_ai"
        assert "Invalid JSON" in (job_log.error_message or "")
        assert job_log.finished_at is not None
        assert mock_session_local.committed is True


@pytest.mark.asyncio
async def test_hourly_job_completes_successfully_on_happy_path(mock_session_local):
    """Contract Test 4: Hourly collection successfully processes batch and records 'success'."""
    with patch("app.scheduler.jobs.collect_new_articles", new_callable=AsyncMock), \
         patch("app.scheduler.jobs.process_collected_articles", new_callable=AsyncMock) as mock_pipeline:
        mock_pipeline.return_value = 7

        job_log = await hourly_collection(
            health_checker=AsyncMock(return_value=True),
            pending_checker=AsyncMock(return_value=7),
        )

        assert job_log.status == "success"
        assert job_log.processed_count == 7
        assert job_log.error_message is None
        assert job_log.finished_at is not None
        assert mock_session_local.committed is True


@pytest.mark.asyncio
async def test_hourly_proceeds_when_primary_down_but_fallback_available(mock_session_local):
    """Regression (Bug 1): Primary 429/503 must NOT skip the hour if fallback model is healthy."""
    with patch("app.scheduler.jobs.collect_new_articles", new_callable=AsyncMock), \
         patch("app.scheduler.jobs.process_collected_articles", new_callable=AsyncMock) as mock_pipeline:
        mock_pipeline.return_value = 4

        # Primary fails with 429, fallback succeeds with 200 OK
        mock_primary = MagicMock()
        mock_primary.generate_json = AsyncMock(side_effect=AIRateLimitError("429 Primary exhausted"))

        mock_fallback = MagicMock()
        mock_fallback.generate_json = AsyncMock(return_value={"ok": True})

        # Pre-flight check must return True because fallback is alive
        is_alive = await check_any_model_available(
            primary_provider=mock_primary,
            fallback_provider=mock_fallback,
        )
        assert is_alive is True

        job_log = await hourly_collection(
            health_checker=AsyncMock(return_value=is_alive),
            pending_checker=AsyncMock(return_value=4),
        )

        assert job_log.status == "success"
        assert job_log.processed_count == 4
        mock_pipeline.assert_called_once()


@pytest.mark.asyncio
async def test_hourly_skips_only_when_both_models_unavailable(mock_session_local):
    """Only skip the hour when BOTH primary and fallback models return 429 or 503."""
    mock_primary = MagicMock()
    mock_primary.generate_json = AsyncMock(side_effect=AIRateLimitError("429 Primary exhausted"))

    mock_fallback = MagicMock()
    mock_fallback.generate_json = AsyncMock(side_effect=AIServiceUnavailableError("503 Fallback unavailable"))

    is_alive = await check_any_model_available(
        primary_provider=mock_primary,
        fallback_provider=mock_fallback,
    )
    assert is_alive is False

    with patch("app.scheduler.jobs.collect_new_articles", new_callable=AsyncMock) as mock_collect, \
         patch("app.scheduler.jobs.process_collected_articles", new_callable=AsyncMock) as mock_pipeline:
        job_log = await hourly_collection(
            health_checker=AsyncMock(return_value=is_alive),
            pending_checker=AsyncMock(return_value=5),
        )

        assert job_log.status == "skipped_all_models_unavailable"
        assert job_log.processed_count == 0
        mock_collect.assert_called_once()
        mock_pipeline.assert_not_called()
        assert mock_session_local.committed is True


@pytest.mark.asyncio
async def test_hourly_collects_but_skips_preflight_when_no_pending_articles(mock_session_local):
    """Ingestion always runs; pre-flight and AI processing are skipped when the queue is empty."""
    mock_health = AsyncMock()
    with patch("app.scheduler.jobs.collect_new_articles", new_callable=AsyncMock) as mock_collect, \
         patch("app.scheduler.jobs.process_collected_articles", new_callable=AsyncMock) as mock_pipeline:
        job_log = await hourly_collection(
            health_checker=mock_health,
            pending_checker=AsyncMock(return_value=0),
        )

        assert job_log.status == "skipped_no_pending"
        assert job_log.processed_count == 0
        mock_collect.assert_called_once()
        mock_health.assert_not_called()
        mock_pipeline.assert_not_called()
        assert mock_session_local.committed is True


@pytest.mark.asyncio
async def test_check_any_model_available_logic(monkeypatch):
    """Verify check_any_model_available implements Option A quota optimization and fallback chaining."""
    monkeypatch.setattr(settings, "gemini_enable_fallback", True)
    monkeypatch.setattr(settings, "gemini_fallback_model", "gemini-3.5-flash-lite")

    # 1. Option A: Fallback succeeds immediately -> Primary is NEVER called (0 primary 20 RPD consumed)
    p_unused = MagicMock()
    p_unused.generate_json = AsyncMock()
    fb_ok = MagicMock()
    fb_ok.generate_json = AsyncMock(return_value={"ok": True})
    assert await check_any_model_available(primary_provider=p_unused, fallback_provider=fb_ok) is True
    fb_ok.generate_json.assert_called_once()
    p_unused.generate_json.assert_not_called()

    # 2. Fallback 503, Primary succeeds -> Returns True
    fb_503 = MagicMock()
    fb_503.generate_json = AsyncMock(side_effect=AIServiceUnavailableError("503 spike"))
    p_ok = MagicMock()
    p_ok.generate_json = AsyncMock(return_value={"ok": True})
    assert await check_any_model_available(primary_provider=p_ok, fallback_provider=fb_503) is True
    fb_503.generate_json.assert_called_once()
    p_ok.generate_json.assert_called_once()

    # 3. Fallback enabled, but both fail -> Returns False
    p_fail = MagicMock()
    p_fail.generate_json = AsyncMock(side_effect=AIRateLimitError("429 primary quota"))
    assert await check_any_model_available(primary_provider=p_fail, fallback_provider=fb_503) is False

    # 4. Fallback disabled -> Tests primary directly; fallback is never called
    monkeypatch.setattr(settings, "gemini_enable_fallback", False)
    fb_unused2 = MagicMock()
    fb_unused2.generate_json = AsyncMock()
    p_ok2 = MagicMock()
    p_ok2.generate_json = AsyncMock(return_value={"ok": True})
    assert await check_any_model_available(primary_provider=p_ok2, fallback_provider=fb_unused2) is True
    p_ok2.generate_json.assert_called_once()
    fb_unused2.generate_json.assert_not_called()

    # 5. Fallback enabled, but prefer_fallback=False -> Tests primary first
    monkeypatch.setattr(settings, "gemini_enable_fallback", True)
    p_ok3 = MagicMock()
    p_ok3.generate_json = AsyncMock(return_value={"ok": True})
    fb_unused3 = MagicMock()
    fb_unused3.generate_json = AsyncMock()
    assert await check_any_model_available(primary_provider=p_ok3, fallback_provider=fb_unused3, prefer_fallback=False) is True
    p_ok3.generate_json.assert_called_once()
    fb_unused3.generate_json.assert_not_called()


@pytest.mark.asyncio
async def test_preflight_calls_separated_from_total_article_calls():
    """Registration 2 Test: Pre-flight calls must not inflate _total_calls or call_count."""
    from app.ai.provider import GeminiProvider

    GeminiProvider.reset_call_counters()
    mock_rotator = MagicMock()
    mock_response = MagicMock()
    mock_response.text = '{"ok": true}'
    mock_rotator.execute = AsyncMock(return_value=mock_response)

    provider = GeminiProvider(
        primary_model="gemini-3.8-flash",
        rotator=mock_rotator,
        enable_fallback=False,
    )

    # 1. Execute pre-flight check call
    res_pre = await provider.generate_json('Respond with {"ok": true}', purpose="preflight")
    assert res_pre == {"ok": True}
    assert GeminiProvider.get_preflight_calls() == 1
    assert GeminiProvider.get_total_calls() == 0
    assert provider.call_count == 0

    # 2. Execute actual article analysis call
    res_real = await provider.generate_json('{"title": "Test News"}', purpose="analysis")
    assert res_real == {"ok": True}
    assert GeminiProvider.get_preflight_calls() == 1
    assert GeminiProvider.get_total_calls() == 1
    assert provider.call_count == 1


@pytest.mark.asyncio
async def test_startup_reconciliation_marks_stale_running_jobs(mock_session_local):
    """Verify startup reconciliation executes update query for stale running jobs."""
    reconciled_count = await reconcile_interrupted_jobs(max_age_minutes=30)
    assert reconciled_count == 1
    assert mock_session_local.committed is True


@pytest.mark.asyncio
async def test_daily_report_skips_when_mock_content_refused(mock_session_local):
    """Verify daily_report job logs 'skipped_mock_content' when MockPublishRefusedError is raised."""
    with patch("app.scheduler.jobs.generate_and_save_daily_report", new_callable=AsyncMock) as mock_gen, \
         patch("app.scheduler.jobs.publish_report", new_callable=AsyncMock) as mock_pub:
        mock_gen.return_value = (datetime.now(UTC).date(), [], "report text")
        mock_pub.side_effect = MockPublishRefusedError("Cannot publish mock content in development")

        job_log = await daily_report()
        assert job_log.status == "skipped_mock_content"
        assert "Cannot publish mock content" in (job_log.error_message or "")
        assert job_log.finished_at is not None
        assert mock_session_local.committed is True


@pytest.mark.asyncio
async def test_daily_report_happy_path(mock_session_local):
    """Verify daily_report job completes with 'success' and records processed chunks."""
    with patch("app.scheduler.jobs.generate_and_save_daily_report", new_callable=AsyncMock) as mock_gen, \
         patch("app.scheduler.jobs.publish_report", new_callable=AsyncMock) as mock_pub:
        mock_gen.return_value = (datetime.now(UTC).date(), [{"event_id": 1}], "report text")
        mock_pub.return_value = [{"message_id": 101}, {"message_id": 102}]

        job_log = await daily_report(dry_run=True)
        assert job_log.status == "success"
        assert job_log.processed_count == 2
        assert job_log.error_message is None
        assert mock_session_local.committed is True


def test_scheduler_configuration_and_overlap_prevention():
    """Verify APScheduler configuration enforces max_instances=1 and coalesce=True for overlap prevention."""
    scheduler = build_scheduler()
    jobs = {j.id: j for j in scheduler.get_jobs()}

    assert "hourly_collection" in jobs
    assert "daily_report" in jobs

    hourly = jobs["hourly_collection"]
    assert hourly.max_instances == 1
    assert hourly.coalesce is True
    assert hourly.misfire_grace_time == 300

    daily = jobs["daily_report"]
    assert daily.max_instances == 1
    assert daily.coalesce is True


@pytest.mark.asyncio
async def test_check_any_model_available_closes_transient_providers(monkeypatch):
    """Providers created inside the pre-flight must be closed when the call returns."""
    monkeypatch.setattr(settings, "gemini_enable_fallback", True)
    monkeypatch.setattr(settings, "gemini_fallback_model", "gemini-3.5-flash-lite")

    created = []

    class FakeProvider:
        def __init__(self, **kwargs):
            self.closed = False
            self.generate_json = AsyncMock(return_value={"ok": True})
            created.append(self)

        def close(self):
            self.closed = True

    monkeypatch.setattr("app.scheduler.jobs.GeminiProvider", FakeProvider)

    assert await check_any_model_available() is True
    assert len(created) == 1
    assert created[0].closed is True


@pytest.mark.asyncio
async def test_check_any_model_available_never_closes_injected_providers():
    """Providers owned by the caller must not be closed by the pre-flight."""
    primary = MagicMock()
    primary.generate_json = AsyncMock(return_value={"ok": True})
    fallback = MagicMock()
    fallback.generate_json = AsyncMock(return_value={"ok": True})

    assert (
        await check_any_model_available(primary_provider=primary, fallback_provider=fallback)
        is True
    )
    primary.close.assert_not_called()
    fallback.close.assert_not_called()


@pytest.mark.asyncio
async def test_end_to_end_scheduler_cycle_with_mock_provider(mock_session_local):
    """End-to-end dry run: Full cycle of hourly_collection + daily_report with MockAIProvider."""
    mock_provider = MockAIProvider(
        canned_response={
            "is_relevant": True,
            "is_injection_attempt": False,
            "categories": ["DIP"],
            "event_title": "Regional Diplomatic Convening",
            "headline_fa": "نشست دیپلماتیک منطقه‌ای",
            "summary_fa": "نشست دیپلماتیک برای تقویت همکاری‌های منطقه‌ای برگزار شد.",
            "importance": 4,
            "verification_status": "confirmed",
        }
    )

    with patch("app.scheduler.jobs.collect_new_articles", new_callable=AsyncMock), \
         patch("app.scheduler.jobs.process_collected_articles", new_callable=AsyncMock) as mock_pipeline, \
         patch("app.scheduler.jobs.generate_and_save_daily_report", new_callable=AsyncMock) as mock_gen, \
         patch("app.scheduler.jobs.publish_report", new_callable=AsyncMock) as mock_pub:
        mock_pipeline.return_value = 3
        mock_gen.return_value = (datetime.now(UTC).date(), [{"event_id": 1}], "mock daily report")
        mock_pub.return_value = [{"message_id": 1001}]

        # 1. Run hourly collection with Mock Provider, 3 pending articles, and mocked health check
        job_hourly = await hourly_collection(
            provider=mock_provider,
            health_checker=AsyncMock(return_value=True),
            pending_checker=AsyncMock(return_value=3),
        )
        assert job_hourly.status == "success"
        assert job_hourly.processed_count == 3
        assert job_hourly.finished_at is not None

        # 2. Run daily report in dry-run mode
        job_daily = await daily_report(dry_run=True)
        assert job_daily.status == "success"
        assert job_daily.processed_count == 1
        assert job_daily.finished_at is not None
