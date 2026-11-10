"""Unit tests for Phase 7.5 Gemini API Rotator, SQLite persistence, and model fallback."""

import os
from pathlib import Path
import tempfile
from unittest.mock import AsyncMock, MagicMock
import pytest

from gemini_rotator import GeminiAPIRotator, MemoryAdapter, SQLiteAdapter
from app.ai.provider import (
    AIProviderError,
    AIRateLimitError,
    GeminiProvider,
)
from app.config import settings


def test_rotator_skips_rate_limited_key():
    """Test 1: Verify key encountering 429 is placed in cooldown and next working key is selected."""
    keys = ["key_alpha", "key_beta", "key_gamma"]
    rotator = GeminiAPIRotator(keys=keys, db=MemoryAdapter())

    # First key picked
    first_key = rotator.get_next_working_key()
    assert first_key == "key_alpha"

    # Simulate 429 Resource Exhausted on first key
    rotator.record_outcome(first_key, exc=Exception("429 RESOURCE_EXHAUSTED: Quota exceeded"))

    # Next working key should skip key_alpha and return key_beta
    second_key = rotator.get_next_working_key()
    assert second_key == "key_beta"
    assert second_key != first_key


def test_rotator_marks_suspended_on_invalid_key():
    """Test 2: Verify API_KEY_INVALID permanently suspends key and removes it from active rotation."""
    keys = ["key_invalid", "key_valid"]
    rotator = GeminiAPIRotator(keys=keys, db=MemoryAdapter())

    # Simulate fatal authentication/permission rejection
    rotator.record_outcome("key_invalid", exc=Exception("API_KEY_INVALID: API key not valid. Please pass a valid API key."))

    # Verify key is marked suspended
    assert "key_invalid" in rotator._suspended
    assert rotator.get_active_count() == 1

    # Key should never be selected in subsequent calls
    for _ in range(5):
        selected = rotator.get_next_working_key()
        assert selected == "key_valid"


def test_rotator_persists_state_across_restart(tmp_path: Path):
    """Test 3: Verify SQLiteAdapter persists suspended key state across rotator re-initialization."""
    db_file = tmp_path / "gemini_keys_test.db"
    keys = ["key_to_suspend", "key_healthy"]

    # Session 1: Mark key suspended and close DB
    adapter1 = SQLiteAdapter(str(db_file))
    rotator1 = GeminiAPIRotator(keys=keys, db=adapter1)
    rotator1.record_outcome("key_to_suspend", exc=Exception("PERMISSION_DENIED: The caller does not have permission"))
    adapter1.close()

    # Session 2 (simulate process restart with same SQLite file):
    adapter2 = SQLiteAdapter(str(db_file))
    rotator2 = GeminiAPIRotator(keys=keys, db=adapter2)

    # Suspended key must be reloaded from SQLite
    assert "key_to_suspend" in rotator2._suspended
    assert rotator2.get_active_count() == 1

    # Picked key must be the healthy key, skipping suspended key
    selected = rotator2.get_next_working_key()
    assert selected == "key_healthy"
    adapter2.close()


@pytest.mark.asyncio
async def test_rotator_falls_back_to_fallback_model_on_quota_exhaustion():
    """Test 4: Verify quota exhaustion on primary model triggers fallback model when enabled."""
    mock_rotator = MagicMock()
    success_resp = MagicMock()
    success_resp.text = '{"status": "recovered_via_fallback", "model_used": "gemini-3.5-flash-lite"}'

    # First attempt (primary model) raises 429 quota exhaustion; second attempt (fallback) succeeds
    mock_rotator.execute = AsyncMock(
        side_effect=[
            Exception("429 RESOURCE_EXHAUSTED: Free tier quota reached for gemini-3.8-flash"),
            success_resp,
        ]
    )

    provider = GeminiProvider(
        rotator=mock_rotator,
        primary_model="gemini-3.8-flash",
        fallback_model="gemini-3.5-flash-lite",
        enable_fallback=True,
    )

    result = await provider.generate_json("Analyze regional developments")

    assert result == {"status": "recovered_via_fallback", "model_used": "gemini-3.5-flash-lite"}
    assert mock_rotator.execute.call_count == 2

    # Verify first call used primary model and second call used fallback model
    calls = mock_rotator.execute.call_args_list
    assert calls[0].kwargs["model"] == "gemini-3.8-flash"
    assert calls[1].kwargs["model"] == "gemini-3.5-flash-lite"


@pytest.mark.asyncio
async def test_generate_json_uses_primary_model():
    """Test 5: Verify generate_json passes settings.gemini_primary_model to rotator."""
    mock_rotator = MagicMock()
    mock_resp = MagicMock()
    mock_resp.text = '{"event_title": "Diplomatic Summit", "is_relevant": true}'
    mock_rotator.execute = AsyncMock(return_value=mock_resp)

    provider = GeminiProvider(rotator=mock_rotator, primary_model=settings.gemini_primary_model)
    result = await provider.generate_json("Summarize the news")

    assert result["is_relevant"] is True
    assert mock_rotator.execute.call_count == 1
    call_kwargs = mock_rotator.execute.call_args.kwargs
    assert call_kwargs["model"] == "gemini-3.8-flash"
    assert provider.model_name == "gemini-3.8-flash"


@pytest.mark.asyncio
async def test_generate_json_returns_parsed_dict():
    """Test 6: Verify generate_json parses JSON string (with markdown code fence) into Python dict."""
    mock_rotator = MagicMock()
    raw_payload = """```json
{
    "is_relevant": true,
    "categories": ["POL", "DIP"],
    "event_title": "Peace Negotiations in Doha",
    "headline_fa": "مذاکرات صلح در دوحه برگزار شد",
    "importance": 4
}
```"""
    mock_resp = MagicMock()
    mock_resp.text = raw_payload
    mock_rotator.execute = AsyncMock(return_value=mock_resp)

    provider = GeminiProvider(rotator=mock_rotator)
    parsed = await provider.generate_json("Sample prompt")

    assert isinstance(parsed, dict)
    assert parsed["is_relevant"] is True
    assert parsed["categories"] == ["POL", "DIP"]
    assert parsed["event_title"] == "Peace Negotiations in Doha"
    assert parsed["importance"] == 4


def test_gemini_provider_default_init_with_sqlite(tmp_path: Path):
    """Verify GeminiProvider default constructor instantiates SQLiteAdapter and can close cleanly."""
    db_file = tmp_path / "sub" / "keys.db"
    provider = GeminiProvider(api_key="AIzaSyTest123", key_db_path=db_file)
    assert provider.model_name == settings.gemini_primary_model
    assert isinstance(provider.rotator, GeminiAPIRotator)
    provider.close()


@pytest.mark.asyncio
async def test_gemini_provider_json_decode_error():
    """Verify GeminiProvider raises AIProviderError on invalid JSON response."""
    mock_rotator = MagicMock()
    mock_resp = MagicMock()
    mock_resp.text = "NOT_VALID_JSON"
    mock_rotator.execute = AsyncMock(return_value=mock_resp)

    provider = GeminiProvider(rotator=mock_rotator)
    with pytest.raises(AIProviderError) as exc_info:
        await provider.generate_json("Test invalid json")
    assert "Invalid JSON" in str(exc_info.value)


@pytest.mark.asyncio
async def test_gemini_provider_service_unavailable_error():
    """Verify GeminiProvider raises AIServiceUnavailableError on 503 or internal errors."""
    from app.ai.provider import AIServiceUnavailableError

    mock_rotator = MagicMock()
    mock_rotator.execute = AsyncMock(side_effect=Exception("503 Backend service temporarily overloaded"))

    provider = GeminiProvider(rotator=mock_rotator)
    with pytest.raises(AIServiceUnavailableError):
        await provider.generate_json("Test prompt")


@pytest.mark.asyncio
async def test_gemini_provider_fallback_failure_raises():
    """Verify GeminiProvider raises when both primary and fallback fail."""
    mock_rotator = MagicMock()
    mock_rotator.execute = AsyncMock(
        side_effect=[
            Exception("429 Quota exhausted"),
            Exception("503 Fallback also failed"),
        ]
    )

    provider = GeminiProvider(
        rotator=mock_rotator,
        enable_fallback=True,
        primary_model="gemini-3.8-flash",
        fallback_model="gemini-3.5-flash-lite",
    )
    from app.ai.provider import AIServiceUnavailableError
    with pytest.raises(AIServiceUnavailableError):
        await provider.generate_json("Test prompt")


@pytest.mark.asyncio
async def test_model_name_reflects_fallback_after_primary_exhausted():
    """Verify provider.model_name dynamically updates to fallback model when fallback succeeds."""
    mock_rotator = MagicMock()
    success_resp = MagicMock()
    success_resp.text = '{"status": "ok_fallback"}'

    # Primary model triggers 429; fallback succeeds
    mock_rotator.execute = AsyncMock(
        side_effect=[
            Exception("429 RESOURCE_EXHAUSTED"),
            success_resp,
        ]
    )

    provider = GeminiProvider(
        rotator=mock_rotator,
        primary_model="gemini-3.8-flash",
        fallback_model="gemini-3.5-flash-lite",
        enable_fallback=True,
    )

    # Initial state
    assert provider.model_name == "gemini-3.8-flash"
    assert provider.primary_model == "gemini-3.8-flash"
    assert provider.fallback_model == "gemini-3.5-flash-lite"

    result = await provider.generate_json("test prompt")
    assert result == {"status": "ok_fallback"}

    # Dynamic model_name MUST now reflect the actual fallback model used!
    assert provider.model_name == "gemini-3.5-flash-lite"


def test_provider_fails_fast_when_no_keys_in_development(monkeypatch):
    """Verify GeminiProvider raises RuntimeError immediately if no keys configured in development/production."""
    from app.config import Settings
    monkeypatch.setattr(settings, "app_env", "development")
    monkeypatch.setattr(settings, "gemini_api_key", "")
    monkeypatch.setattr(Settings, "get_gemini_keys", lambda self: [])

    with pytest.raises(RuntimeError, match="No Gemini API keys configured"):
        GeminiProvider()


@pytest.mark.asyncio
async def test_all_keys_suspended_raises_ai_rate_limit_error():
    """Verify RuntimeError from rotator when all keys are suspended translates to AIRateLimitError."""
    mock_rotator = MagicMock()
    mock_rotator.execute = AsyncMock(
        side_effect=RuntimeError("No API keys available — all keys are suspended. Check rotator.status() for details.")
    )

    provider = GeminiProvider(rotator=mock_rotator, enable_fallback=False)

    with pytest.raises(AIRateLimitError) as exc_info:
        await provider.generate_json("Test prompt")
    assert "all keys are suspended" in str(exc_info.value)


@pytest.mark.asyncio
async def test_provider_auxiliary_and_defensive_branches(tmp_path: Path, monkeypatch):
    """Verify auxiliary defensive branches: markdown trimming, null responses, error translations, and mock provider."""
    from app.ai.provider import (
        is_retryable_ai_error,
        _is_rate_limit_or_quota,
        _translate_and_raise,
        AIServiceUnavailableError,
        MockAIProvider,
        get_ai_provider,
    )
    from gemini_rotator.middleware import ErrorType

    # 1. Error translation branches
    assert is_retryable_ai_error(Exception("429 resource_exhausted")) is True
    assert _is_rate_limit_or_quota(AIRateLimitError("Quota limit hit")) is True

    with pytest.raises(AIProviderError):
        _translate_and_raise(AIProviderError("Custom AI Provider Error"))

    with pytest.raises(AIServiceUnavailableError):
        _translate_and_raise(Exception("503 Service Unavailable"))

    # 2. Markdown trimming without 'json' prefix (``` { ... } ```)
    mock_rotator = MagicMock()
    mock_resp = MagicMock()
    mock_resp.text = "```\n{\"trimmed\": true}\n```"
    mock_rotator.execute = AsyncMock(return_value=mock_resp)
    p = GeminiProvider(rotator=mock_rotator)
    res = await p.generate_json("test")
    assert res == {"trimmed": True}

    # 3. None response text fallback to "{}"
    mock_resp_none = MagicMock()
    mock_resp_none.text = None
    mock_rotator.execute = AsyncMock(return_value=mock_resp_none)
    res_none = await p.generate_json("test")
    assert res_none == {}

    # 4. MockAIProvider set_canned_response
    mock_p = MockAIProvider()
    mock_p.set_canned_response({"custom": 123})
    assert await mock_p.generate_json("p") == {"custom": 123}

    # 5. GeminiProvider with app_env == 'test' and empty keys creates mock_key_unconfigured
    from app.config import Settings
    monkeypatch.setattr(settings, "app_env", "test")
    monkeypatch.setattr(settings, "gemini_api_key", "")
    monkeypatch.setattr(Settings, "get_gemini_keys", lambda self: [])
    p_test = GeminiProvider(key_db_path=tmp_path / "test_keys.db")
    assert p_test.model_name == settings.gemini_primary_model
    p_test.close()

    # 6. get_ai_provider returns MockAIProvider in test environment
    assert isinstance(get_ai_provider(), MockAIProvider)


@pytest.mark.asyncio
async def test_last_used_model_resets_when_both_models_fail():
    """Verify _last_used_model resets to primary_model when both primary and fallback fail."""
    mock_rotator = MagicMock()
    mock_rotator.execute = AsyncMock(
        side_effect=[
            Exception("429 Quota exhausted for primary"),
            Exception("429 Quota exhausted for fallback"),
        ]
    )

    provider = GeminiProvider(
        rotator=mock_rotator,
        primary_model="gemini-3.8-flash",
        fallback_model="gemini-3.5-flash-lite",
        enable_fallback=True,
    )

    with pytest.raises(AIRateLimitError):
        await provider.generate_json("test prompt")

    # model_name must be reset to primary model, not left corrupted on fallback
    assert provider.model_name == "gemini-3.8-flash"


@pytest.mark.asyncio
async def test_last_used_model_never_fallback_when_disabled():
    """Verify _last_used_model never changes to fallback when enable_fallback is False."""
    mock_rotator = MagicMock()
    mock_rotator.execute = AsyncMock(side_effect=Exception("429 Quota exhausted"))

    provider = GeminiProvider(
        rotator=mock_rotator,
        primary_model="gemini-3.8-flash",
        fallback_model="gemini-3.5-flash-lite",
        enable_fallback=False,
    )

    with pytest.raises(AIRateLimitError):
        await provider.generate_json("test prompt")

    assert provider.model_name == "gemini-3.8-flash"
    assert mock_rotator.execute.call_count == 1


def test_init_fails_when_primary_equals_fallback():
    """Verify GeminiProvider.__init__ raises ValueError when primary_model equals fallback_model and fallback is enabled."""
    with pytest.raises(ValueError, match="primary_model and fallback_model must differ"):
        GeminiProvider(
            primary_model="gemini-3.8-flash",
            fallback_model="gemini-3.8-flash",
            enable_fallback=True,
        )


@pytest.mark.asyncio
async def test_fallback_triggers_on_503_unavailable():
    """Regression test: 503 UNAVAILABLE on primary model must trigger fallback to fallback_model."""
    mock_rotator = MagicMock()
    error_503 = Exception("503 UNAVAILABLE: High demand spike on gemini-3.8-flash")
    success = MagicMock()
    success.text = '{"status": "ok_via_fallback_after_503"}'
    mock_rotator.execute = AsyncMock(side_effect=[error_503, success])

    provider = GeminiProvider(
        rotator=mock_rotator,
        primary_model="gemini-3.8-flash",
        fallback_model="gemini-3.5-flash-lite",
        enable_fallback=True,
    )

    result = await provider.generate_json("test prompt")
    assert result == {"status": "ok_via_fallback_after_503"}
    assert provider.model_name == "gemini-3.5-flash-lite"
    assert mock_rotator.execute.call_count == 2





