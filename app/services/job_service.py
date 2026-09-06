from __future__ import annotations

import asyncio
import json

from sqlalchemy import select

from app.config import get_settings
from app.db import DownloadJob, JobEvent, JobStatus, MediaMetadata, SessionLocal, User
from app.errors import classify_error
from app.security import canonicalize_url, redact_secrets
from app.services.downloader import MediaFormat, MediaInfo

settings = get_settings()
_reservation_lock = asyncio.Lock()
_TERMINAL = {JobStatus.COMPLETED.value, JobStatus.FAILED.value, JobStatus.CANCELLED.value}


def serialize_formats(info: MediaInfo) -> str:
    return json.dumps(
        [
            {
                "format_id": fmt.format_id,
                "height": fmt.height,
                "ext": fmt.ext,
                "fps": fmt.fps,
                "tbr": fmt.tbr,
            }
            for fmt in info.formats
        ],
        separators=(",", ":"),
    )


def deserialize_formats(formats_json: str) -> list[MediaFormat]:
    try:
        payload = json.loads(formats_json or "[]")
    except (json.JSONDecodeError, TypeError):
        return []
    result: list[MediaFormat] = []
    for item in payload if isinstance(payload, list) else []:
        try:
            height = int(item["height"])
        except (KeyError, TypeError, ValueError):
            continue
        result.append(
            MediaFormat(
                format_id=str(item.get("format_id") or ""),
                height=height,
                ext=item.get("ext"),
                fps=float(item["fps"]) if item.get("fps") else None,
                tbr=float(item["tbr"]) if item.get("tbr") else None,
            )
        )
    return sorted(result, key=lambda item: item.height)


def deserialize_qualities(formats_json: str) -> list[int]:
    return sorted({item.height for item in deserialize_formats(formats_json)})


async def ensure_dashboard_user() -> User:
    async with SessionLocal() as session:
        user = await session.scalar(select(User).where(User.telegram_id == 0))
        if user is None:
            user = User(
                telegram_id=0,
                username="dashboard",
                language="en",
                is_allowed=True,
                is_admin=True,
            )
            session.add(user)
            await session.commit()
            await session.refresh(user)
        return user


async def _active_existing(session, user_id: int, normalized: str) -> DownloadJob | None:
    return await session.scalar(
        select(DownloadJob)
        .join(MediaMetadata, MediaMetadata.job_id == DownloadJob.id)
        .where(
            DownloadJob.user_id == user_id,
            MediaMetadata.normalized_url == normalized,
            DownloadJob.status.not_in(_TERMINAL),
        )
        .order_by(DownloadJob.id.desc())
    )


async def reserve_analysis_job(
    *,
    user_id: int,
    chat_id: int,
    source_url: str,
    progress_message_id: int | None = None,
    source: str = "telegram",
    priority: int = 0,
) -> tuple[DownloadJob, bool]:
    """Atomically reserve one active Job per normalized URL/user in single-service mode."""
    normalized = canonicalize_url(source_url)
    async with _reservation_lock:
        async with SessionLocal() as session:
            existing = await _active_existing(session, user_id, normalized)
            if existing is not None:
                session.expunge(existing)
                return existing, False

            job = DownloadJob(
                user_id=user_id,
                chat_id=chat_id,
                progress_message_id=progress_message_id,
                source_url=source_url,
                platform="unknown",
                status=JobStatus.ANALYZING.value,
            )
            session.add(job)
            await session.flush()
            session.add(
                MediaMetadata(
                    job_id=job.id,
                    formats_json="[]",
                    normalized_url=normalized,
                    source=source,
                    priority=int(priority),
                )
            )
            session.add(JobEvent(job_id=job.id, event_type=JobStatus.ANALYZING.value, message="URL reserved"))
            await session.commit()
            await session.refresh(job)
            session.expunge(job)
            return job, True


async def create_analyzed_job(
    *,
    user_id: int,
    chat_id: int,
    source_url: str,
    info: MediaInfo,
    progress_message_id: int | None = None,
    source: str = "telegram",
    priority: int = 0,
) -> DownloadJob:
    normalized = canonicalize_url(source_url)
    async with _reservation_lock:
        async with SessionLocal() as session:
            existing = await _active_existing(session, user_id, normalized)
            if existing is not None:
                session.expunge(existing)
                return existing

            job = DownloadJob(
                user_id=user_id,
                chat_id=chat_id,
                progress_message_id=progress_message_id,
                source_url=source_url,
                platform=info.platform,
                title=info.title,
                duration=info.duration,
                thumbnail=info.thumbnail,
                status=JobStatus.READY.value,
            )
            session.add(job)
            await session.flush()
            session.add(
                MediaMetadata(
                    job_id=job.id,
                    uploader=info.uploader,
                    formats_json=serialize_formats(info),
                    normalized_url=normalized,
                    is_playlist=info.is_playlist,
                    playlist_count=info.playlist_count,
                    source=source,
                    priority=int(priority),
                )
            )
            session.add(JobEvent(job_id=job.id, event_type=JobStatus.READY.value, message="pre-analyzed media"))
            await session.commit()
            await session.refresh(job)
            session.expunge(job)
            return job


async def _existing_media_info(job: DownloadJob) -> MediaInfo:
    async with SessionLocal() as session:
        metadata = await session.get(MediaMetadata, job.id)
        formats = deserialize_formats(metadata.formats_json if metadata else "[]")
        return MediaInfo(
            title=job.title or "Analyzing",
            thumbnail=job.thumbnail,
            duration=job.duration,
            uploader=metadata.uploader if metadata else None,
            platform=job.platform,
            formats=formats,
            qualities=sorted({fmt.height for fmt in formats}),
            webpage_url=job.source_url,
            is_playlist=bool(metadata.is_playlist) if metadata else False,
            playlist_count=metadata.playlist_count if metadata else 0,
        )


async def analyze_and_create_job(
    *,
    user_id: int,
    chat_id: int,
    source_url: str,
    progress_message_id: int | None = None,
    source: str = "telegram",
    priority: int = 0,
    downloader_service=None,
) -> tuple[DownloadJob, MediaInfo]:
    from app.services.downloader import get_downloader_service

    job, created = await reserve_analysis_job(
        user_id=user_id,
        chat_id=chat_id,
        source_url=source_url,
        progress_message_id=progress_message_id,
        source=source,
        priority=priority,
    )
    if not created:
        return job, await _existing_media_info(job)

    service = downloader_service or get_downloader_service()
    try:
        info = await asyncio.to_thread(service.probe, source_url)
        if info.duration and info.duration > settings.max_video_duration_seconds:
            raise ValueError(f"Duration limit exceeded ({settings.max_video_duration_seconds}s)")
        async with SessionLocal() as session:
            db_job = await session.get(DownloadJob, job.id)
            metadata = await session.get(MediaMetadata, job.id)
            if db_job is None or metadata is None:
                raise LookupError("Reserved job disappeared")
            if db_job.status == JobStatus.CANCELLED.value:
                raise RuntimeError("Analysis cancelled")
            db_job.title = info.title
            db_job.thumbnail = info.thumbnail
            db_job.duration = info.duration
            db_job.platform = info.platform
            db_job.status = JobStatus.READY.value
            metadata.uploader = info.uploader
            metadata.formats_json = serialize_formats(info)
            metadata.is_playlist = info.is_playlist
            metadata.playlist_count = info.playlist_count
            session.add(
                JobEvent(
                    job_id=job.id,
                    event_type=JobStatus.READY.value,
                    message=f"qualities={info.qualities}; playlist={info.is_playlist}",
                )
            )
            await session.commit()
            await session.refresh(db_job)
            session.expunge(db_job)
            return db_job, info
    except Exception as exc:
        error = classify_error(exc)
        safe = redact_secrets(
            str(exc),
            bot_token=settings.bot_token,
            database_url=settings.database_url,
        )[:1500]
        async with SessionLocal() as session:
            db_job = await session.get(DownloadJob, job.id)
            if db_job is not None and db_job.status != JobStatus.CANCELLED.value:
                db_job.status = JobStatus.FAILED.value
                db_job.error = f"{error.code.value}: {safe}"
                session.add(JobEvent(job_id=job.id, event_type=error.code.value, message="analysis failed"))
                await session.commit()
        raise


async def queue_existing_job(job_id: int, quality: str, *, priority: int | None = None) -> None:
    async with SessionLocal() as session:
        job = await session.get(DownloadJob, job_id)
        if job is None:
            raise LookupError("Job not found")
        if job.status not in {JobStatus.READY.value, JobStatus.FAILED.value}:
            raise ValueError("Job is not queueable")
        metadata = await session.get(MediaMetadata, job_id)
        if quality not in {"best", "audio", "mp3"}:
            available = deserialize_qualities(metadata.formats_json if metadata else "[]")
            height = int(quality.removesuffix("p"))
            if height not in available:
                alternatives = ", ".join(f"{item}p" for item in available) or "Best / MP3"
                raise ValueError(
                    f"Requested format {height}p is not available. Available: {alternatives}"
                )
        job.selected_quality = "audio" if quality == "mp3" else quality
        job.status = JobStatus.QUEUED.value
        job.error = None
        job.progress = 0.0
        effective_priority = int(priority if priority is not None else (metadata.priority if metadata else 0))
        if metadata is not None:
            metadata.priority = effective_priority
        await session.commit()

    from app.queue import enqueue_download

    await enqueue_download(job_id, priority=effective_priority)


async def get_job_user_id(job_id: int) -> int:
    async with SessionLocal() as session:
        value = await session.scalar(select(DownloadJob.user_id).where(DownloadJob.id == job_id))
        if value is None:
            raise LookupError("Job not found")
        return int(value)


async def get_job_priority(job_id: int) -> int:
    async with SessionLocal() as session:
        value = await session.scalar(select(MediaMetadata.priority).where(MediaMetadata.job_id == job_id))
        return int(value or 0)
