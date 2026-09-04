from functools import lru_cache
from pathlib import Path

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Required for Telegram.
    bot_token: str = ""

    # Telegram / networking. Polling works without a public URL or webhook configuration.
    app_mode: str = "polling"
    webhook_base_url: str = ""
    webhook_secret: str = ""

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

    # Internal media budget can be larger than Telegram's delivery budget because Phase 5
    # can adaptively transcode before upload.
    max_file_size_mb: int = 512
    telegram_upload_limit_mb: int = 49
    auto_compress_enabled: bool = True
    media_compression_attempts: int = 2
    progress_update_seconds: float = 2.0
    default_language: str = "ar"

    # yt-dlp engine tuning. All values are optional and conservative by default.
    ytdlp_socket_timeout_seconds: int = 30
    ytdlp_retries: int = 2
    ytdlp_fragment_retries: int = 3
    ytdlp_concurrent_fragments: int = 4

    # Job reliability. These are optional and have production-safe defaults.
    job_max_retries: int = 2
    job_retry_base_seconds: float = 5.0

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
        return value if value in {"polling", "webhook"} else "polling"

    @field_validator("queue_backend")
    @classmethod
    def validate_queue_backend(cls, value: str) -> str:
        value = value.lower().strip()
        return value if value in {"inline", "redis"} else "inline"

    @field_validator("default_language")
    @classmethod
    def validate_language(cls, value: str) -> str:
        return value if value in {"ar", "en"} else "ar"

    @field_validator("job_max_retries", "ytdlp_retries", "ytdlp_fragment_retries")
    @classmethod
    def validate_retry_count(cls, value: int) -> int:
        return max(0, min(int(value), 10))

    @field_validator("job_retry_base_seconds")
    @classmethod
    def validate_retry_delay(cls, value: float) -> float:
        return max(1.0, min(float(value), 60.0))

    @field_validator("ytdlp_socket_timeout_seconds")
    @classmethod
    def validate_socket_timeout(cls, value: int) -> int:
        return max(5, min(int(value), 120))

    @field_validator("ytdlp_concurrent_fragments")
    @classmethod
    def validate_fragment_concurrency(cls, value: int) -> int:
        return max(1, min(int(value), 16))

    @field_validator("media_compression_attempts")
    @classmethod
    def validate_compression_attempts(cls, value: int) -> int:
        return max(1, min(int(value), 4))

    @field_validator("max_file_size_mb")
    @classmethod
    def validate_internal_file_limit(cls, value: int) -> int:
        return max(16, min(int(value), 4096))

    @field_validator("telegram_upload_limit_mb")
    @classmethod
    def validate_upload_limit(cls, value: int) -> int:
        return max(1, min(int(value), 2048))

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

    @property
    def telegram_upload_limit_bytes(self) -> int:
        return self.telegram_upload_limit_mb * 1024 * 1024


@lru_cache
def get_settings() -> Settings:
    """Load settings without touching the filesystem.

    Runtime directories are created by the downloader when a job actually starts.
    Keeping this function side-effect free makes imports safe in CI, tests, CLI tools,
    and read-only environments.
    """
    return Settings()
