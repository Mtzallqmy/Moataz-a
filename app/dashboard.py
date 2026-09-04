from __future__ import annotations

import asyncio
import secrets
from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Request, WebSocket, WebSocketDisconnect, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select

from app.config import get_settings
from app.db import DownloadJob, JobEvent, JobStatus, SessionLocal, User, WorkerNode
from app.jobs import RUNNING_STATUSES

settings = get_settings()
router = APIRouter()
security = HTTPBasic(auto_error=False)
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def _dashboard_ws_token() -> str:
    # One optional password is enough for a simple deployment. A dedicated token can still be set.
    return settings.dashboard_ws_token or settings.dashboard_password


def require_admin(credentials: HTTPBasicCredentials | None = Depends(security)) -> str:
    # No dashboard secret configured => dashboard is disabled, not exposed with a default password.
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


async def _snapshot() -> dict:
    async with SessionLocal() as session:
        total_jobs = await session.scalar(select(func.count()).select_from(DownloadJob)) or 0
        active_jobs = await session.scalar(
            select(func.count())
            .select_from(DownloadJob)
            .where(DownloadJob.status.in_(list(RUNNING_STATUSES)))
        ) or 0
        completed_jobs = await session.scalar(
            select(func.count())
            .select_from(DownloadJob)
            .where(DownloadJob.status == JobStatus.COMPLETED.value)
        ) or 0
        failed_jobs = await session.scalar(
            select(func.count())
            .select_from(DownloadJob)
            .where(DownloadJob.status == JobStatus.FAILED.value)
        ) or 0
        user_count = await session.scalar(select(func.count()).select_from(User)) or 0
        jobs = (
            await session.scalars(select(DownloadJob).order_by(DownloadJob.id.desc()).limit(30))
        ).all()
        users = (await session.scalars(select(User).order_by(User.id.desc()).limit(50))).all()
        workers = (
            await session.scalars(select(WorkerNode).order_by(WorkerNode.last_seen.desc()).limit(20))
        ).all()
        return {
            "stats": {
                "total_jobs": total_jobs,
                "active_jobs": active_jobs,
                "completed_jobs": completed_jobs,
                "failed_jobs": failed_jobs,
                "users": user_count,
            },
            "jobs": [
                {
                    "id": job.id,
                    "title": job.title or "—",
                    "platform": job.platform,
                    "quality": job.selected_quality or "—",
                    "status": job.status,
                    "progress": round(job.progress or 0, 1),
                    "speed": job.speed or "—",
                    "eta": job.eta or "—",
                    "error": job.error,
                    "worker_id": job.worker_id,
                    "file_size": job.file_size,
                }
                for job in jobs
            ],
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
        }


@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request, _: str = Depends(require_admin)):
    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context={"app_name": settings.app_name, "ws_token": _dashboard_ws_token()},
    )


@router.get("/api/dashboard")
async def dashboard_data(_: str = Depends(require_admin)):
    return await _snapshot()


@router.get("/api/jobs/{job_id}/events")
async def dashboard_job_events(job_id: int, _: str = Depends(require_admin)):
    async with SessionLocal() as session:
        job = await session.get(DownloadJob, job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Job not found")
        events = list(
            await session.scalars(
                select(JobEvent)
                .where(JobEvent.job_id == job_id)
                .order_by(JobEvent.id.desc())
                .limit(100)
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


@router.post("/dashboard/users")
async def update_user_access(
    telegram_id: int = Form(...),
    allowed: bool = Form(False),
    language: str = Form("ar"),
    _: str = Depends(require_admin),
):
    if language not in {"ar", "en"}:
        language = "ar"
    async with SessionLocal() as session:
        user = await session.scalar(select(User).where(User.telegram_id == telegram_id))
        if user is None:
            user = User(
                telegram_id=telegram_id,
                language=language,
                is_allowed=allowed,
                is_admin=telegram_id in settings.admin_ids,
            )
            session.add(user)
        else:
            user.is_allowed = allowed
            user.language = language
        await session.commit()
    return RedirectResponse("/dashboard", status_code=303)


@router.websocket("/ws/jobs")
async def jobs_ws(websocket: WebSocket):
    expected = _dashboard_ws_token()
    token = websocket.query_params.get("token", "")
    if not settings.dashboard_password or not expected or not secrets.compare_digest(token, expected):
        await websocket.close(code=4401)
        return
    await websocket.accept()
    try:
        while True:
            await websocket.send_json(await _snapshot())
            await asyncio.sleep(2)
    except WebSocketDisconnect:
        return
