"""Pytest configuration and fixtures for Middle East News Bot."""

import os
import pytest

# Ensure safe test environment defaults before any app import
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://newsbot:test_pass@localhost:5432/newsbot_test")
os.environ.setdefault("GEMINI_API_KEY", "test_gemini_key_12345")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123456789:TEST_BOT_TOKEN_XYZ")
os.environ.setdefault("TELEGRAM_CHANNEL_ID", "-1009876543210")
os.environ.setdefault("TELEGRAM_ADMIN_CHAT_ID", "987654321")
