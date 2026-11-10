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

