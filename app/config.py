from functools import lru_cache
from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    bot_token: str = ""
    app_mode: str = "polling"
    webhook_base_url: str = ""
    webhook_secret: str = "change-me"
    telegram_local_api_url: str = ""

    admin_telegram_ids: str = ""
    allowed_telegram_ids: str = ""

    app_name: str = "Moataz Media Bot"
    worker_name: str = ""
    app_host: str = "0.0.0.0"
    app_port: int = 8000
    public_base_url: str = "http://localhost:8000"
    download_dir: Path = Path("/data/downloads")
    max_concurrent_jobs: int = 2
    max_jobs_per_user: int = 1
    max_video_duration_seconds: int = 14400
    max_file_size_mb: int = 1900
    progress_update_seconds: float = 2.0
    default_language: str = "ar"

    dashboard_username: str = "admin"
    dashboard_password: str = "change-me-now"
    dashboard_ws_token: str = "change-me-too"

    database_url: str = "sqlite+aiosqlite:///./moataz.db"
    redis_url: str = "redis://localhost:6379/0"
    ytdlp_cookies_file: str = ""

    @property
    def admin_ids(self) -> set[int]:
        return {int(part.strip()) for part in self.admin_telegram_ids.split(",") if part.strip()}

    @property
    def allowed_ids(self) -> set[int]:
        return {int(part.strip()) for part in self.allowed_telegram_ids.split(",") if part.strip()}

    @field_validator("app_mode")
    @classmethod
    def validate_mode(cls, value: str) -> str:
        value = value.lower().strip()
        if value not in {"polling", "webhook"}:
            raise ValueError("APP_MODE must be 'polling' or 'webhook'")
        return value

    @field_validator("default_language")
    @classmethod
    def validate_language(cls, value: str) -> str:
        return value if value in {"ar", "en"} else "ar"

    @property
    def webhook_url(self) -> str:
        return f"{self.webhook_base_url.rstrip('/')}/telegram/webhook"

    @property
    def max_file_size_bytes(self) -> int:
        return self.max_file_size_mb * 1024 * 1024


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    settings.download_dir.mkdir(parents=True, exist_ok=True)
    return settings
