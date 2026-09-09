from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime
from enum import StrEnum

from sqlalchemy import BigInteger, Boolean, DateTime, Float, ForeignKey, Integer, String, Text, func
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from app.config import get_settings

settings = get_settings()
engine = create_async_engine(settings.database_url, pool_pre_ping=True)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


class JobStatus(StrEnum):
    PENDING = "PENDING"
    ANALYZING = "ANALYZING"
    READY = "READY"
    QUEUED = "QUEUED"
    DOWNLOADING = "DOWNLOADING"
    MERGING = "MERGING"
    PROCESSING = "PROCESSING"
    CUTTING = "CUTTING"
    UPLOADING = "UPLOADING"
    RETRYING = "RETRYING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    PROBING = "PROBING"


class ProjectStatus(StrEnum):
    DRAFT = "DRAFT"
    READY = "READY"
    RENDERING = "RENDERING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class RenderStatus(StrEnum):
    QUEUED = "QUEUED"
    PREPARING = "PREPARING"
    RENDERING = "RENDERING"
    UPLOADING = "UPLOADING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    username: Mapped[str | None] = mapped_column(String(255), nullable=True)
    language: Mapped[str] = mapped_column(String(5), default="ar")
    is_allowed: Mapped[bool] = mapped_column(Boolean, default=True)
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    jobs: Mapped[list[DownloadJob]] = relationship(back_populates="user")


class DownloadJob(Base):
    __tablename__ = "download_jobs"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    chat_id: Mapped[int] = mapped_column(BigInteger)
    progress_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    source_url: Mapped[str] = mapped_column(Text)
    platform: Mapped[str] = mapped_column(String(32), default="unknown")
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    duration: Mapped[int | None] = mapped_column(Integer, nullable=True)
    thumbnail: Mapped[str | None] = mapped_column(Text, nullable=True)
    selected_quality: Mapped[str | None] = mapped_column(String(32), nullable=True)
    cut_start: Mapped[float | None] = mapped_column(Float, nullable=True)
    cut_end: Mapped[float | None] = mapped_column(Float, nullable=True)
    worker_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(32), default=JobStatus.PENDING.value, index=True)
    progress: Mapped[float] = mapped_column(Float, default=0.0)
    speed: Mapped[str | None] = mapped_column(String(64), nullable=True)
    eta: Mapped[str | None] = mapped_column(String(64), nullable=True)
    output_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    file_size: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    user: Mapped[User] = relationship(back_populates="jobs")
    events: Mapped[list[JobEvent]] = relationship(back_populates="job", cascade="all, delete-orphan")


class MediaMetadata(Base):
    __tablename__ = "media_metadata"

    job_id: Mapped[int] = mapped_column(ForeignKey("download_jobs.id"), primary_key=True)
    uploader: Mapped[str | None] = mapped_column(Text, nullable=True)
    formats_json: Mapped[str] = mapped_column(Text, default="[]")
    normalized_url: Mapped[str] = mapped_column(Text, index=True)
    is_playlist: Mapped[bool] = mapped_column(Boolean, default=False)
    playlist_count: Mapped[int] = mapped_column(Integer, default=0)
    cut_mode: Mapped[str] = mapped_column(String(16), default="PRECISE")
    source: Mapped[str] = mapped_column(String(16), default="telegram")
    priority: Mapped[int] = mapped_column(Integer, default=0)


class JobEvent(Base):
    __tablename__ = "job_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("download_jobs.id"), index=True)
    event_type: Mapped[str] = mapped_column(String(64), index=True)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    job: Mapped[DownloadJob] = relationship(back_populates="events")


class WorkerNode(Base):
    __tablename__ = "worker_nodes"

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    hostname: Mapped[str] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(32), default="ONLINE")
    active_jobs: Mapped[int] = mapped_column(Integer, default=0)
    last_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class MediaAsset(Base):
    __tablename__ = "media_assets"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    asset_type: Mapped[str] = mapped_column(String(32), index=True)
    source_type: Mapped[str] = mapped_column(String(32), index=True)
    source_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    telegram_file_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    local_path: Mapped[str] = mapped_column(Text)
    mime_type: Mapped[str | None] = mapped_column(String(255), nullable=True)
    duration: Mapped[float | None] = mapped_column(Float, nullable=True)
    width: Mapped[int | None] = mapped_column(Integer, nullable=True)
    height: Mapped[int | None] = mapped_column(Integer, nullable=True)
    file_size: Mapped[int] = mapped_column(BigInteger)
    metadata_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class MediaProject(Base):
    __tablename__ = "media_projects"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    chat_id: Mapped[int] = mapped_column(BigInteger)
    name: Mapped[str] = mapped_column(String(255), default="Media Project")
    status: Mapped[str] = mapped_column(String(32), default=ProjectStatus.DRAFT.value, index=True)
    aspect_ratio: Mapped[str] = mapped_column(String(16), default="9:16")
    width: Mapped[int] = mapped_column(Integer, default=1080)
    height: Mapped[int] = mapped_column(Integer, default=1920)
    fps: Mapped[int] = mapped_column(Integer, default=30)
    timeline_json: Mapped[str] = mapped_column(Text, default='{"version":1,"tracks":[]}')
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    project_assets: Mapped[list[ProjectAsset]] = relationship(
        back_populates="project", cascade="all, delete-orphan", order_by="ProjectAsset.position"
    )
    render_jobs: Mapped[list[RenderJob]] = relationship(
        back_populates="project", cascade="all, delete-orphan"
    )


class ProjectAsset(Base):
    __tablename__ = "project_assets"

    project_id: Mapped[int] = mapped_column(
        ForeignKey("media_projects.id", ondelete="CASCADE"), primary_key=True
    )
    asset_id: Mapped[int] = mapped_column(ForeignKey("media_assets.id"), primary_key=True)
    position: Mapped[int] = mapped_column(Integer, default=0, index=True)
    role: Mapped[str] = mapped_column(String(32), default="main")

    project: Mapped[MediaProject] = relationship(back_populates="project_assets")
    asset: Mapped[MediaAsset] = relationship()


class RenderJob(Base):
    __tablename__ = "render_jobs"

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("media_projects.id"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    status: Mapped[str] = mapped_column(String(32), default=RenderStatus.QUEUED.value, index=True)
    progress: Mapped[float] = mapped_column(Float, default=0.0)
    output_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    file_size: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    project: Mapped[MediaProject] = relationship(back_populates="render_jobs")


async def init_db() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def get_session() -> AsyncIterator[AsyncSession]:
    async with SessionLocal() as session:
        yield session
