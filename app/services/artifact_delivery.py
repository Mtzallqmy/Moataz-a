"""Short-lived download links for original public artifact files."""
from __future__ import annotations

import hashlib
import hmac
import os
import re
import time
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from app.config import Settings, get_settings
from app.db import DownloadJob, JobStatus, SessionLocal
from app.services.artifact_urls import ARTIFACT_PLATFORMS

router = APIRouter()


def _base_url(settings: Settings) -> str | None:
    raw = settings.artifact_public_base_url.strip() or os.getenv("RAILWAY_PUBLIC_DOMAIN", "").strip()
    if not raw:
        return None
    if "://" not in raw:
        raw = f"https://{raw}"
    try:
        parsed = urlsplit(raw)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.path not in {"", "/"} or parsed.query or parsed.fragment or parsed.port not in {None, 443}:
            return None
    except ValueError:
        return None
    return raw.rstrip("/")


def _signature(settings: Settings, job_id: int, expires: int) -> str:
    key = hashlib.sha256(b"moataz-artifact-delivery-v1\0" + settings.bot_token.encode()).digest()
    return hmac.new(key, f"{job_id}:{expires}".encode(), hashlib.sha256).hexdigest()


def artifact_download_link(settings: Settings, job_id: int, *, now: int | None = None) -> str | None:
    base = _base_url(settings)
    if not base or not settings.bot_token:
        return None
    expires = (int(time.time()) if now is None else now) + min(settings.artifact_link_ttl_seconds, settings.temp_retention_seconds)
    return f"{base}/files/artifacts/{job_id}?expires={expires}&token={_signature(settings, job_id, expires)}"


def valid_artifact_token(settings: Settings, job_id: int, expires: int, token: str, *, now: int | None = None) -> bool:
    current = int(time.time()) if now is None else now
    return bool(settings.bot_token and job_id > 0 and current <= expires <= current + settings.artifact_link_ttl_seconds and re.fullmatch(r"[0-9a-f]{64}", token) is not None and hmac.compare_digest(_signature(settings, job_id, expires), token))


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
