"""HTTP delivery for short-lived public artifact links (server only)."""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from app.config import Settings, get_settings
from app.db import DownloadJob, JobStatus, SessionLocal
from app.services.artifact_links import valid_artifact_token
from app.services.artifact_urls import ARTIFACT_PLATFORMS

router = APIRouter()


def artifact_file_path(settings: Settings, job: DownloadJob) -> Path:
    if job.status != JobStatus.COMPLETED.value or job.platform not in ARTIFACT_PLATFORMS or not job.output_path or job.chat_id == 0:
        raise HTTPException(status_code=404, detail="File not available")
    try:
        expected = settings.download_dir.resolve(strict=True) / str(job.id) / "artifact"
        root = expected.resolve(strict=True)
        path = Path(job.output_path).resolve(strict=True)
        if root != expected or not path.is_file() or path.parent != root or path.stat().st_size != job.file_size:
            raise ValueError("Invalid artifact path")
    except (OSError, ValueError):
        raise HTTPException(status_code=404, detail="File not available") from None
    return path


@router.api_route("/files/artifacts/{job_id}", methods=["GET", "HEAD"])
async def download_artifact(job_id: int, expires: int, token: str):
    settings = get_settings()
    if not valid_artifact_token(settings, job_id, expires, token):
        raise HTTPException(status_code=404, detail="File or link expired")
    async with SessionLocal() as session:
        job = await session.get(DownloadJob, job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="File not available")
        path = artifact_file_path(settings, job)
    return FileResponse(path, filename=path.name, media_type="application/octet-stream", headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer"})
