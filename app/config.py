from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    database_url: str = "postgresql+asyncpg://newsbot:CHANGE_THIS_PASSWORD@localhost:5432/newsbot"
    gemini_api_key: str = ""
    gemini_model: str = "gemini-2.5-flash"
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

    def validate_runtime(self) -> list[str]:
        missing = []
        if not self.gemini_api_key or "YOUR_GEMINI_API_KEY" in self.gemini_api_key:
            missing.append("GEMINI_API_KEY")
        if not self.telegram_bot_token or "YOUR_TELEGRAM_BOT_TOKEN" in self.telegram_bot_token:
            missing.append("TELEGRAM_BOT_TOKEN")
        if not self.telegram_channel_id or self.telegram_channel_id == "-1001234567890":
            missing.append("TELEGRAM_CHANNEL_ID")
        if not self.telegram_admin_chat_id:
            missing.append("TELEGRAM_ADMIN_CHAT_ID")
        return missing

settings = Settings()
