from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    database_url: str
    gemini_api_key: str
    gemini_model: str = "gemini-2.5-flash"
    telegram_bot_token: str
    telegram_channel_id: str
    telegram_admin_chat_id: int
    timezone: str = "Asia/Tehran"
    collection_interval_minutes: int = 60
    daily_report_hour: int = 8
    daily_report_minute: int = 0
    max_articles_per_batch: int = 50
    min_importance: int = 2
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

settings = Settings()
