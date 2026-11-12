"""Unit tests for app.config Settings."""

import ssl

import certifi

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


def test_database_url_for_asyncpg_strips_sslmode_only():
    """asyncpg must not receive sslmode, while psycopg keeps the untouched DSN."""
    settings = Settings(
        database_url="postgresql+asyncpg://u:p@h:5432/db?sslmode=require&application_name=newsbot"
    )

    asyncpg_url = settings.database_url_for_asyncpg

    assert "sslmode" not in asyncpg_url
    assert "application_name=newsbot" in asyncpg_url
    assert "sslmode=require" in settings.database_url


def test_asyncpg_require_encrypts_without_certificate_validation():
    """libpq require only encrypts; it must not fail on Aiven's private CA."""
    settings = Settings(database_url="postgresql+asyncpg://u:p@h:5432/db?sslmode=require")
    context = settings.asyncpg_connect_args["ssl"]

    assert isinstance(context, ssl.SSLContext)
    assert context.verify_mode == ssl.CERT_NONE
    assert context.check_hostname is False


def test_asyncpg_verify_ca_validates_chain_without_hostname():
    """verify-ca checks the certificate chain but not the hostname."""
    settings = Settings(database_url="postgresql+asyncpg://u:p@h:5432/db?sslmode=verify-ca")
    context = settings.asyncpg_connect_args["ssl"]

    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is False


def test_asyncpg_verify_full_also_checks_hostname():
    """verify-full validates both the chain and the hostname."""
    settings = Settings(database_url="postgresql+asyncpg://u:p@h:5432/db?sslmode=verify-full")
    context = settings.asyncpg_connect_args["ssl"]

    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True


def test_asyncpg_root_cert_upgrades_require_to_real_validation():
    """A CA file turns require into verified TLS without any code change."""
    settings = Settings(
        database_url="postgresql+asyncpg://u:p@h:5432/db?sslmode=require",
        postgres_sslrootcert=certifi.where(),
    )
    context = settings.asyncpg_connect_args["ssl"]

    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is False
    assert context.get_ca_certs()


def test_asyncpg_connect_args_ignores_absent_or_disabled_ssl():
    """No TLS requirement means no connect args are passed."""
    assert Settings(database_url="postgresql+asyncpg://u:p@h:5432/db").asyncpg_connect_args == {}
    assert (
        Settings(database_url="postgresql+asyncpg://u:p@h:5432/db?sslmode=disable").asyncpg_connect_args
        == {}
    )
