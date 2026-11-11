"""Unit tests for app.config Settings."""

import pytest
from app.config import Settings


def test_settings_default_values():
    settings = Settings(
        database_url="postgresql+asyncpg://test:test@localhost:5432/test",
        gemini_api_key="test_key",
        telegram_bot_token="test_token",
        telegram_channel_id="@testchannel",
        telegram_admin_chat_id=123456,
    )
    assert settings.timezone == "Asia/Tehran"
    assert settings.collection_interval_minutes == 60
    assert settings.daily_report_hour == 8
    assert settings.daily_report_minute == 0
    assert settings.max_articles_per_batch == 50
    assert settings.min_importance == 2
    assert settings.min_summary_len_for_full_text == 800
    assert settings.require_full_text_for_ai is True


def test_settings_validate_runtime_missing():
    # Settings with placeholders should report missing
    settings = Settings(
        gemini_api_key="YOUR_GEMINI_API_KEY",
        telegram_bot_token="",
        telegram_channel_id="-1001234567890",
        telegram_admin_chat_id=0,
    )
    missing = settings.validate_runtime()
    assert "GEMINI_API_KEY" in missing
    assert "TELEGRAM_BOT_TOKEN" in missing
    assert "TELEGRAM_CHANNEL_ID" in missing
    assert "TELEGRAM_ADMIN_CHAT_ID" in missing


def test_settings_validate_runtime_valid():
    settings = Settings(
        gemini_api_key="actual_secret_gemini_key",
        telegram_bot_token="123456:actual_token",
        telegram_channel_id="-1001999999999",
        telegram_admin_chat_id=5551234,
    )
    missing = settings.validate_runtime()
    assert len(missing) == 0


def test_settings_comma_separated_gemini_keys():
    """Verify GEMINI_API_KEY supports comma-separated list of keys."""
    settings = Settings(
        gemini_api_key="AIzaKeyOne, AIzaKeyTwo, AIzaKeyThree",
    )
    keys = settings.get_gemini_keys()
    assert keys == ["AIzaKeyOne", "AIzaKeyTwo", "AIzaKeyThree"]
