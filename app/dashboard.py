from __future__ import annotations

import asyncio
import secrets
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from app.config import get_settings
from app.db import DownloadJob, JobEvent, JobStatus, MediaMetadata, SessionLocal, User, WorkerNode
from app.errors import classify_error
from app.jobs import RUNNING_STATUSES, set_job_status
from app.operations import readiness_snapshot
from app.queue import cancel_download
from app.rate_limit import dashboard_write_limiter
from app.services.downloader import get_downloader_service
from app.services.job_service import (
    analyze_and_create_job,
    ensure_dashboard_user,
    queue_existing_job,
)
from app.services.urls import parse_bulk_urls
from app.version import RELEASE

settings = get_settings()
router = APIRouter()
security = HTTPBasic(auto_error=False)
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
downloader = get_downloader_service()


class AnalyzeRequest(BaseModel):
    urls: str = Field(min_length=1, max_length=20_000)


class DownloadItem(BaseModel):
    job_id: int
    quality: str = "best"


class DownloadRequest(BaseModel):
    items: list[DownloadItem] = Field(min_length=1, max_length=50)


class PlaylistRequest(BaseModel):
    quality: str = "best"


def require_admin(credentials: HTTPBasicCredentials | None = Depends(security)) -> str:
    if not settings.dashboard_password:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Dashboard disabled")
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unauthorized",
            headers={"WWW-Authenticate": "Basic"},
        )
    valid_user = secrets.compare_digest(credentials.username, settings.dashboard_username)
    valid_password = secrets.compare_digest(credentials.password, settings.dashboard_password)
    if not (valid_user and valid_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unauthorized",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username


async def enforce_write_rate(request: Request) -> None:
    client = request.client.host if request.client else "unknown"
    if not await dashboard_write_limiter.allow(f"web:{client}"):
        raise HTTPException(status_code=429, detail="Rate limit exceeded")


def _serialize_job(job: DownloadJob, metadata: MediaMetadata | None) -> dict:
    return {
        "id": job.id,
        "title": job.title or "—",
        "platform": job.platform,
        "quality": job.selected_quality or "—",
        "status": job.status,
        "progress": round(job.progress or 0.0, 1),
        "speed": job.speed or "—",
        "eta": job.eta or "—",
        "error": job.error,
        "worker_id": job.worker_id,
        "file_size": job.file_size,
        "thumbnail": job.thumbnail,
        "duration": job.duration,
        "uploader": metadata.uploader if metadata else None,
        "playlist": bool(metadata.is_playlist) if metadata else False,
        "playlist_count": metadata.playlist_count if metadata else 0,
        "source": metadata.source if metadata else "telegram",
        "download_ready": job.status == JobStatus.COMPLETED.value and bool(job.output_path),
    }


async def _snapshot() -> dict:
    async with SessionLocal() as session:
        total_jobs = await session.scalar(select(func.count()).select_from(DownloadJob)) or 0
        active_jobs = await session.scalar(
            select(func.count()).select_from(DownloadJob).where(DownloadJob.status.in_(RUNNING_STATUSES))
        ) or 0
        completed = await session.scalar(
            select(func.count()).select_from(DownloadJob).where(DownloadJob.status == JobStatus.COMPLETED.value)
        ) or 0
        failed = await session.scalar(
            select(func.count()).select_from(DownloadJob).where(DownloadJob.status == JobStatus.FAILED.value)
        ) or 0
        user_count = await session.scalar(select(func.count()).select_from(User).where(User.telegram_id != 0)) or 0
        rows = list(
            (
                await session.execute(
                    select(DownloadJob, MediaMetadata)
                    .outerjoin(MediaMetadata, MediaMetadata.job_id == DownloadJob.id)
                    .order_by(DownloadJob.id.desc())
                    .limit(100)
                )
            ).all()
        )
        users = list(await session.scalars(select(User).where(User.telegram_id != 0).order_by(User.id.desc()).limit(100)))
        workers = list(await session.scalars(select(WorkerNode).order_by(WorkerNode.last_seen.desc()).limit(30)))
        jobs = [_serialize_job(job, metadata) for job, metadata in rows]
    system = await readiness_snapshot()
    return {
        "release": RELEASE,
        "stats": {
            "total_jobs": total_jobs,
            "active_jobs": active_jobs,
            "completed_jobs": completed,
            "failed_jobs": failed,
            "users": user_count,
        },
        "jobs": jobs,
        "errors": [job for job in jobs if job["status"] == JobStatus.FAILED.value][:30],
        "users": [
            {
                "telegram_id": user.telegram_id,
                "username": user.username or "—",
                "language": user.language,
                "allowed": user.is_allowed,
                "admin": user.is_admin,
            }
            for user in users
        ],
        "workers": [
            {
                "id": worker.id,
                "hostname": worker.hostname,
                "status": worker.status,
                "active_jobs": worker.active_jobs,
                "last_seen": worker.last_seen.isoformat() if worker.last_seen else None,
            }
            for worker in workers
        ],
        "system": system,
    }


@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request, _: str = Depends(require_admin)):
    return templates.TemplateResponse(request=request, name="dashboard.html", context={"app_name": settings.app_name})


@router.get("/api/dashboard")
async def dashboard_data(_: str = Depends(require_admin)):
    return await _snapshot()


@router.post("/api/media/analyze")
async def analyze_media(payload: AnalyzeRequest, request: Request, _: str = Depends(require_admin)):
    await enforce_write_rate(request)
    parsed = parse_bulk_urls(payload.urls, limit=settings.max_bulk_urls)
    if not parsed.urls:
        raise HTTPException(status_code=400, detail="No valid public URLs found")
    user = await ensure_dashboard_user()
    results: list[dict] = []
    for url in parsed.urls:
        try:
            job, info = await analyze_and_create_job(
                user_id=user.id,
                chat_id=0,
                source_url=url,
                source="dashboard",
            )
            results.append(
                {
                    "job_id": job.id,
                    "url": url,
                    "title": info.title,
                    "thumbnail": info.thumbnail,
                    "duration": info.duration,
                    "uploader": info.uploader,
                    "platform": info.platform,
                    "qualities": info.qualities,
                    "is_playlist": info.is_playlist,
                    "playlist_count": info.playlist_count,
                    "max_playlist_items": settings.max_playlist_items,
                }
            )
        except Exception as exc:
            info = classify_error(exc)
            results.append({"url": url, "error": info.code.value})
    return {"items": results, "duplicates": parsed.duplicates, "rejected": parsed.rejected}


@router.post("/api/media/download")
async def download_media(payload: DownloadRequest, request: Request, _: str = Depends(require_admin)):
    await enforce_write_rate(request)
    queued: list[int] = []
    errors: list[dict] = []
    seen: set[int] = set()
    for item in payload.items:
        if item.job_id in seen:
            continue
        seen.add(item.job_id)
        try:
            await queue_existing_job(item.job_id, item.quality)
            queued.append(item.job_id)
        except Exception as exc:
            errors.append({"job_id": item.job_id, "error": str(exc)[:200]})
    return {"queued": queued, "errors": errors}


@router.post("/api/media/playlist/{job_id}/expand")
async def dashboard_expand_playlist(job_id: int, payload: PlaylistRequest, request: Request, _: str = Depends(require_admin)):
    await enforce_write_rate(request)
    async with SessionLocal() as session:
        parent = await session.get(DownloadJob, job_id)
        metadata = await session.get(MediaMetadata, job_id)
        if parent is None or metadata is None or not metadata.is_playlist:
            raise HTTPException(status_code=404, detail="Playlist job not found")
        if metadata.playlist_count > settings.max_playlist_items:
            raise HTTPException(status_code=400, detail=f"Playlist exceeds safe limit {settings.max_playlist_items}")
        parent_url = parent.source_url
        user_id = parent.user_id
    try:
        entries = await asyncio.to_thread(downloader.expand_playlist, parent_url, limit=settings.max_playlist_items)
    except Exception as exc:
        info = classify_error(exc)
        raise HTTPException(status_code=400, detail=info.code.value) from exc
    child_ids: list[int] = []
    for entry in entries:
        child, _child_info = await analyze_and_create_job(
            user_id=user_id,
            chat_id=0,
            source_url=entry.url,
            source="dashboard",
        )
        if child.status == JobStatus.READY.value:
            await queue_existing_job(child.id, payload.quality)
        child_ids.append(child.id)
    await set_job_status(job_id, JobStatus.COMPLETED, progress=100.0, event_message=f"expanded children={child_ids}")
    return {"parent_job_id": job_id, "children": child_ids}


@router.post("/api/jobs/{job_id}/cancel")
async def dashboard_cancel(job_id: int, request: Request, _: str = Depends(require_admin)):
    await enforce_write_rate(request)
    async with SessionLocal() as session:
        job = await session.get(DownloadJob, job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Job not found")
        if job.status in {JobStatus.COMPLETED.value, JobStatus.FAILED.value, JobStatus.CANCELLED.value}:
            return {"job_id": job_id, "status": job.status}
    await set_job_status(job_id, JobStatus.CANCELLED, error="CANCELLED", event_message="cancel requested from dashboard")
    await cancel_download(job_id)
    return {"job_id": job_id, "status": JobStatus.CANCELLED.value}


@router.get("/api/jobs/{job_id}/file")
async def dashboard_file(job_id: int, _: str = Depends(require_admin)):
    async with SessionLocal() as session:
        job = await session.get(DownloadJob, job_id)
        if job is None or job.status != JobStatus.COMPLETED.value or not job.output_path:
            raise HTTPException(status_code=404, detail="File not ready")
        path = Path(job.output_path)
    root = settings.download_dir.resolve()
    try:
        resolved = path.resolve(strict=True)
        resolved.relative_to(root)
    except (OSError, ValueError):
        raise HTTPException(status_code=404, detail="File not available") from None
    return FileResponse(resolved, filename=resolved.name)


@router.get("/api/jobs/{job_id}/events")
async def job_events(job_id: int, _: str = Depends(require_admin)):
    async with SessionLocal() as session:
        events = list(
            await session.scalars(
                select(JobEvent).where(JobEvent.job_id == job_id).order_by(JobEvent.id.desc()).limit(100)
            )
        )
    return {
        "job_id": job_id,
        "events": [
            {
                "id": event.id,
                "type": event.event_type,
                "message": event.message,
                "created_at": event.created_at.isoformat() if event.created_at else None,
            }
            for event in events
        ],
    }
