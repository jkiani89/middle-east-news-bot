import os
import re

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_env: str = "development"
    database_url: str = "postgresql+asyncpg://newsbot:CHANGE_THIS_PASSWORD@localhost:5432/newsbot"
    gemini_api_key: str = ""
    gemini_primary_model: str = "gemini-3.8-flash"
    gemini_fallback_model: str = "gemini-3.5-flash-lite"
    gemini_enable_fallback: bool = True
    gemini_key_db_path: str = "data/gemini_keys.db"  # DEPRECATED (F-1): ignored; SQLite removed, state in PostgreSQL
    gemini_log_retention_days: int = 30  # Retention days for gemini_request_log cleanup
    gemini_model: str = "gemini-3.8-flash"
    telegram_bot_token: str = ""
    telegram_channel_id: str = ""
    telegram_admin_chat_id: int = 0
    timezone: str = "Asia/Tehran"
    collection_interval_minutes: int = 60
    daily_report_hour: int = 8
    daily_report_minute: int = 0
    max_articles_per_batch: int = 50
    min_importance: int = 2
    min_summary_len_for_full_text: int = 800
    require_full_text_for_ai: bool = True

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore"
    )

    @field_validator("database_url", mode="before")
    @classmethod
    def fix_database_url(cls, v: str) -> str:
        if not isinstance(v, str):
            return v
        # فقط پیشوند postgres:// را اصلاح می‌کنیم
        if v.startswith("postgres://"):
            v = v.replace("postgres://", "postgresql+asyncpg://", 1)
        elif v.startswith("postgresql://"):
            v = v.replace("postgresql://", "postgresql+asyncpg://", 1)

        # تغییر sslmode حذف شد تا psycopg و asyncpg هر دو بتوانند از آن استفاده کنند
        return v

    def get_gemini_keys(self) -> list[str]:
        """Extract all configured Gemini API keys from environment and settings."""
        keys = []
        # Check numbered keys: GEMINI_KEY_1, GEMINI_KEY_2, ...
        for env_k, env_v in sorted(os.environ.items()):
            if re.match(r"^GEMINI_KEY_\d+$", env_k) and env_v:
                for item in env_v.split(","):
                    val = item.strip()
                    if val and val not in ("AIza...", "YOUR_GEMINI_API_KEY") and val not in keys:
                        keys.append(val)

        # Check GEMINI_API_KEY (supports single key or comma-separated keys: key1, key2, ...)
        if self.gemini_api_key:
            for item in self.gemini_api_key.split(","):
                val = item.strip()
                if val and val not in ("AIza...", "YOUR_GEMINI_API_KEY") and val not in keys:
                    keys.append(val)

        return keys

    def validate_runtime(self) -> list[str]:
        missing = []
        if not self.get_gemini_keys():
            missing.append("GEMINI_API_KEY")
        if not self.telegram_bot_token or "YOUR_TELEGRAM_BOT_TOKEN" in self.telegram_bot_token:
            missing.append("TELEGRAM_BOT_TOKEN")
        if not self.telegram_channel_id or self.telegram_channel_id == "-1001234567890":
            missing.append("TELEGRAM_CHANNEL_ID")
        if not self.telegram_admin_chat_id:
            missing.append("TELEGRAM_ADMIN_CHAT_ID")
        return missing

settings = Settings()
