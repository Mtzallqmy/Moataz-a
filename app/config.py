from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime settings.

    BOT_TOKEN and DATABASE_URL are the only variables needed for the media bot.
    OPENAI_BASE_URL and OPENAI_API_TOKEN are optional and enable AI chat when both
    are configured. DASHBOARD_PASSWORD is optional and disables the dashboard
    when empty.
    """

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    bot_token: str = ""
    database_url: str = "sqlite+aiosqlite:///./moataz.db"
    openai_base_url: str = ""
    openai_api_token: str = ""

    app_name: str = "Moataz Media Bot"
    app_host: str = "0.0.0.0"
    app_port: int = Field(default=8000, validation_alias=AliasChoices("PORT", "APP_PORT"))
    download_dir: Path = Path("/data/downloads")
    default_language: str = "ar"

    max_concurrent_jobs: int = 2
    max_jobs_per_user: int = 1
    max_bulk_urls: int = 10
    max_playlist_items: int = 10
    max_video_duration_seconds: int = 14_400
    max_file_size_mb: int = 512
    telegram_upload_limit_mb: int = 49
    progress_update_seconds: float = 2.5
    temp_retention_seconds: int = 21_600

    ytdlp_socket_timeout_seconds: int = 30
    ytdlp_retries: int = 2
    ytdlp_fragment_retries: int = 3
    ytdlp_concurrent_fragments: int = 4
    job_max_retries: int = 2
    job_retry_base_seconds: float = 4.0
    job_retry_cap_seconds: float = 45.0

    ffmpeg_timeout_seconds: int = 900
    ffmpeg_kill_grace_seconds: float = 3.0
    stderr_limit_bytes: int = 16_384

    ai_request_timeout_seconds: int = 120
    ai_max_history_messages: int = 20
    ai_max_reply_chars: int = 12_000

    dashboard_username: str = "admin"
    dashboard_password: str = ""

    @field_validator("database_url", mode="before")
    @classmethod
    def normalize_database_url(cls, value: str) -> str:
        url = str(value or "").strip()
        if not url:
            return "sqlite+aiosqlite:///./moataz.db"
        if url.startswith("postgres://"):
            return "postgresql+asyncpg://" + url[len("postgres://") :]
        if url.startswith("postgresql://") and "+asyncpg" not in url:
            return "postgresql+asyncpg://" + url[len("postgresql://") :]
        return url

    @field_validator("openai_base_url", mode="before")
    @classmethod
    def normalize_openai_base_url(cls, value: str) -> str:
        url = str(value or "").strip().rstrip("/")
        if url and not url.startswith(("https://", "http://")):
            raise ValueError("OPENAI_BASE_URL must use HTTP or HTTPS")
        return url

    @field_validator("default_language")
    @classmethod
    def validate_language(cls, value: str) -> str:
        return value if value in {"ar", "en"} else "ar"

    @field_validator("max_concurrent_jobs")
    @classmethod
    def validate_global_concurrency(cls, value: int) -> int:
        return max(1, min(int(value), 16))

    @field_validator("max_jobs_per_user")
    @classmethod
    def validate_user_concurrency(cls, value: int) -> int:
        return max(1, min(int(value), 4))

    @field_validator("max_bulk_urls")
    @classmethod
    def validate_bulk_limit(cls, value: int) -> int:
        return max(1, min(int(value), 50))

    @field_validator("max_playlist_items")
    @classmethod
    def validate_playlist_limit(cls, value: int) -> int:
        return max(1, min(int(value), 100))

    @field_validator("max_file_size_mb")
    @classmethod
    def validate_file_limit(cls, value: int) -> int:
        return max(16, min(int(value), 4096))

    @field_validator("telegram_upload_limit_mb")
    @classmethod
    def validate_telegram_limit(cls, value: int) -> int:
        return max(1, min(int(value), 2048))

    @field_validator("ytdlp_retries", "ytdlp_fragment_retries", "job_max_retries")
    @classmethod
    def validate_retry_count(cls, value: int) -> int:
        return max(0, min(int(value), 10))

    @field_validator("ytdlp_concurrent_fragments")
    @classmethod
    def validate_fragments(cls, value: int) -> int:
        return max(1, min(int(value), 16))

    @field_validator("job_retry_base_seconds")
    @classmethod
    def validate_retry_base(cls, value: float) -> float:
        return max(0.25, min(float(value), 60.0))

    @field_validator("ai_request_timeout_seconds")
    @classmethod
    def validate_ai_timeout(cls, value: int) -> int:
        return max(10, min(int(value), 600))

    @field_validator("ai_max_history_messages")
    @classmethod
    def validate_ai_history(cls, value: int) -> int:
        return max(2, min(int(value), 50))

    @property
    def max_file_size_bytes(self) -> int:
        return self.max_file_size_mb * 1024 * 1024

    @property
    def telegram_upload_limit_bytes(self) -> int:
        return self.telegram_upload_limit_mb * 1024 * 1024

    @property
    def ai_enabled(self) -> bool:
        return bool(self.openai_base_url and self.openai_api_token)


@lru_cache
def get_settings() -> Settings:
    return Settings()
