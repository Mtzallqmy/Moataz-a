from functools import lru_cache
from pathlib import Path
from urllib.parse import urlparse

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # The only value that is always required at runtime.
    bot_token: str = ""

    # Telegram / networking. Polling works without a public URL or webhook configuration.
    app_mode: str = "polling"
    webhook_base_url: str = ""
    webhook_secret: str = ""
    telegram_local_api_url: str = ""

    # Empty lists mean open access. Set either list only when you want a private bot.
    admin_telegram_ids: str = ""
    allowed_telegram_ids: str = ""

    # Application defaults. Railway injects PORT automatically, while APP_PORT remains supported.
    app_name: str = "Moataz Media Bot"
    worker_name: str = ""
    app_host: str = "0.0.0.0"
    app_port: int = Field(default=8000, validation_alias=AliasChoices("PORT", "APP_PORT"))
    public_base_url: str = ""
    download_dir: Path = Path("/data/downloads")
    max_concurrent_jobs: int = 2
    max_jobs_per_user: int = 1
    max_video_duration_seconds: int = 14400
    max_file_size_mb: int = 1900
    progress_update_seconds: float = 2.0
    default_language: str = "ar"

    # Dashboard is optional. These defaults do not block the bot from starting.
    dashboard_username: str = "admin"
    dashboard_password: str = ""
    dashboard_ws_token: str = ""

    # DATABASE_URL may be a normal Railway/Postgres URL; it is normalized for asyncpg below.
    # SQLite remains a local-development fallback, but Railway users should set DATABASE_URL.
    database_url: str = "sqlite+aiosqlite:///./moataz.db"

    # Inline jobs are the default, so Redis is not required for a single-service deployment.
    # Advanced deployments may set QUEUE_BACKEND=redis and REDIS_URL.
    queue_backend: str = "inline"
    redis_url: str = ""

    ytdlp_cookies_file: str = ""

    @property
    def admin_ids(self) -> set[int]:
        return {int(part.strip()) for part in self.admin_telegram_ids.split(",") if part.strip()}

    @property
    def allowed_ids(self) -> set[int]:
        return {int(part.strip()) for part in self.allowed_telegram_ids.split(",") if part.strip()}

    @property
    def access_restricted(self) -> bool:
        """Whether an allowlist/admin list was explicitly configured."""
        return bool(self.admin_ids or self.allowed_ids)

    @field_validator("app_mode")
    @classmethod
    def validate_mode(cls, value: str) -> str:
        value = value.lower().strip()
        if value not in {"polling", "webhook"}:
            raise ValueError("APP_MODE must be 'polling' or 'webhook'")
        return value

    @field_validator("telegram_local_api_url", mode="before")
    @classmethod
    def validate_telegram_local_api_url(cls, value: str) -> str:
        """Use a custom Bot API server only when it is an absolute HTTP(S) URL.

        Railway projects often retain old optional variables. An accidental value such as
        a service name must never replace Telegram's official API endpoint.
        """
        raw = str(value or "").strip().rstrip("/")
        if not raw:
            return ""
        try:
            parsed = urlparse(raw)
        except ValueError:
            return ""
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            return ""
        return raw

    @field_validator("queue_backend")
    @classmethod
    def validate_queue_backend(cls, value: str) -> str:
        value = value.lower().strip()
        return value if value in {"inline", "redis"} else "inline"

    @field_validator("default_language")
    @classmethod
    def validate_language(cls, value: str) -> str:
        return value if value in {"ar", "en"} else "ar"

    @field_validator("database_url", mode="before")
    @classmethod
    def normalize_database_url(cls, value: str) -> str:
        """Accept Railway/Heroku-style Postgres URLs with SQLAlchemy's asyncpg engine."""
        url = str(value or "").strip()
        if not url:
            return "sqlite+aiosqlite:///./moataz.db"
        if url.startswith("postgres://"):
            return "postgresql+asyncpg://" + url[len("postgres://") :]
        if url.startswith("postgresql://"):
            return "postgresql+asyncpg://" + url[len("postgresql://") :]
        return url

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
