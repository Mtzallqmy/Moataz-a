from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import func, select

from app.db import DownloadJob, JobEvent, JobStatus, SessionLocal

ACTIVE_STATUSES = {
    JobStatus.PENDING.value,
    JobStatus.ANALYZING.value,
    JobStatus.PROBING.value,
    JobStatus.READY.value,
    JobStatus.QUEUED.value,
    JobStatus.RETRYING.value,
    JobStatus.DOWNLOADING.value,
    JobStatus.MERGING.value,
    JobStatus.PROCESSING.value,
    JobStatus.CUTTING.value,
    JobStatus.UPLOADING.value,
}

RUNNING_STATUSES = {
    JobStatus.QUEUED.value,
    JobStatus.RETRYING.value,
    JobStatus.DOWNLOADING.value,
    JobStatus.MERGING.value,
    JobStatus.PROCESSING.value,
    JobStatus.CUTTING.value,
    JobStatus.UPLOADING.value,
}

TERMINAL_STATUSES = {
    JobStatus.COMPLETED.value,
    JobStatus.FAILED.value,
    JobStatus.CANCELLED.value,
}


@dataclass(frozen=True, slots=True)
class ErrorInfo:
    code: str
    retryable: bool


def classify_job_error(exc: BaseException) -> ErrorInfo:
    """Classify failures without coupling the core job layer to one library.

    Matching is intentionally conservative: authentication/private/deleted media,
    invalid input, format errors, and file-size failures should not be retried.
    Network/timeout/temporary upstream failures can be retried.
    """

    name = type(exc).__name__.lower()
    message = str(exc).lower()
    text = f"{name} {message}"

    non_retryable_patterns = (
        "private video",
        "video unavailable",
        "not available",
        "has been removed",
        "sign in to confirm",
        "login required",
        "cookies",
        "unsupported url",
        "invalid url",
        "requested format is not available",
        "file exceeds",
        "above max_file_size_mb",
        "duration limit",
        "ffmpeg",
    )
    if any(pattern in text for pattern in non_retryable_patterns):
        if "file exceeds" in text or "max_file_size" in text:
            return ErrorInfo("FILE_TOO_LARGE", False)
        if "ffmpeg" in text:
            return ErrorInfo("MEDIA_PROCESSING", False)
        if "private" in text or "login" in text or "cookies" in text or "sign in" in text:
            return ErrorInfo("ACCESS_REQUIRED", False)
        if "unsupported" in text or "invalid url" in text:
            return ErrorInfo("INVALID_SOURCE", False)
        if "format" in text:
            return ErrorInfo("FORMAT_UNAVAILABLE", False)
        return ErrorInfo("MEDIA_UNAVAILABLE", False)

    if "telegram" in text and any(
        marker in text for marker in ("network", "timeout", "connection", "clienterror")
    ):
        return ErrorInfo("TELEGRAM_NETWORK", True)

    retryable_patterns = (
        "timeout",
        "timed out",
        "network",
        "connection reset",
        "connection refused",
        "temporary failure",
        "temporarily unavailable",
        "remote disconnected",
        "http error 429",
        "http error 500",
        "http error 502",
        "http error 503",
        "http error 504",
        "server disconnected",
    )
    if any(pattern in text for pattern in retryable_patterns):
        return ErrorInfo("NETWORK", True)

    if "downloaderror" in name:
        return ErrorInfo("EXTRACTOR", False)

    return ErrorInfo("UNKNOWN", False)


async def record_job_event(job_id: int, event_type: str, message: str | None = None) -> None:
    async with SessionLocal() as session:
        session.add(
            JobEvent(
                job_id=job_id,
                event_type=event_type[:64],
                message=(message[:2000] if message else None),
            )
        )
        await session.commit()


async def set_job_status(
    job_id: int,
    status: JobStatus | str,
    *,
    error: str | None = None,
    progress: float | None = None,
    event_message: str | None = None,
) -> bool:
    value = status.value if isinstance(status, JobStatus) else str(status)
    async with SessionLocal() as session:
        job = await session.get(DownloadJob, job_id)
        if job is None:
            return False
        job.status = value
        if error is not None:
            job.error = error[:2000]
        if progress is not None:
            job.progress = max(0.0, min(100.0, float(progress)))
        if value == JobStatus.COMPLETED.value:
            job.completed_at = datetime.now(UTC)
        session.add(
            JobEvent(
                job_id=job_id,
                event_type=value[:64],
                message=(event_message[:2000] if event_message else None),
            )
        )
        await session.commit()
        return True


async def count_user_running_jobs(user_id: int, *, exclude_job_id: int | None = None) -> int:
    conditions = [
        DownloadJob.user_id == user_id,
        DownloadJob.status.in_(RUNNING_STATUSES),
    ]
    if exclude_job_id is not None:
        conditions.append(DownloadJob.id != exclude_job_id)
    async with SessionLocal() as session:
        value = await session.scalar(
            select(func.count()).select_from(DownloadJob).where(*conditions)
        )
        return int(value or 0)


async def is_job_cancelled(job_id: int) -> bool:
    async with SessionLocal() as session:
        status = await session.scalar(select(DownloadJob.status).where(DownloadJob.id == job_id))
        return status == JobStatus.CANCELLED.value
