from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import func, select

from app.db import DownloadJob, JobEvent, JobStatus, SessionLocal
from app.errors import ErrorInfo, classify_error

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
TERMINAL_STATUSES = {JobStatus.COMPLETED.value, JobStatus.FAILED.value, JobStatus.CANCELLED.value}

ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    JobStatus.PENDING.value: {JobStatus.ANALYZING.value, JobStatus.QUEUED.value, JobStatus.CANCELLED.value},
    JobStatus.ANALYZING.value: {JobStatus.READY.value, JobStatus.FAILED.value, JobStatus.CANCELLED.value},
    JobStatus.PROBING.value: {JobStatus.READY.value, JobStatus.FAILED.value, JobStatus.CANCELLED.value},
    JobStatus.READY.value: {JobStatus.QUEUED.value, JobStatus.COMPLETED.value, JobStatus.CANCELLED.value},
    JobStatus.QUEUED.value: {JobStatus.DOWNLOADING.value, JobStatus.FAILED.value, JobStatus.CANCELLED.value},
    JobStatus.DOWNLOADING.value: {
        JobStatus.MERGING.value,
        JobStatus.PROCESSING.value,
        JobStatus.CUTTING.value,
        JobStatus.UPLOADING.value,
        JobStatus.RETRYING.value,
        JobStatus.COMPLETED.value,
        JobStatus.FAILED.value,
        JobStatus.CANCELLED.value,
    },
    JobStatus.MERGING.value: {
        JobStatus.PROCESSING.value,
        JobStatus.CUTTING.value,
        JobStatus.UPLOADING.value,
        JobStatus.FAILED.value,
        JobStatus.CANCELLED.value,
    },
    JobStatus.PROCESSING.value: {
        JobStatus.CUTTING.value,
        JobStatus.UPLOADING.value,
        JobStatus.FAILED.value,
        JobStatus.CANCELLED.value,
    },
    JobStatus.CUTTING.value: {JobStatus.UPLOADING.value, JobStatus.FAILED.value, JobStatus.CANCELLED.value},
    JobStatus.UPLOADING.value: {
        JobStatus.RETRYING.value,
        JobStatus.COMPLETED.value,
        JobStatus.FAILED.value,
        JobStatus.CANCELLED.value,
    },
    JobStatus.RETRYING.value: {
        JobStatus.DOWNLOADING.value,
        JobStatus.UPLOADING.value,
        JobStatus.FAILED.value,
        JobStatus.CANCELLED.value,
    },
    JobStatus.FAILED.value: {JobStatus.QUEUED.value},
    JobStatus.CANCELLED.value: set(),
    JobStatus.COMPLETED.value: set(),
}


def can_transition(current: str, target: str) -> bool:
    return current == target or target in ALLOWED_TRANSITIONS.get(current, set())


def classify_job_error(exc: BaseException) -> ErrorInfo:
    return classify_error(exc)


async def record_job_event(job_id: int, event_type: str, message: str | None = None) -> None:
    async with SessionLocal() as session:
        session.add(JobEvent(job_id=job_id, event_type=event_type[:64], message=message[:2000] if message else None))
        await session.commit()


async def set_job_status(
    job_id: int,
    status: JobStatus | str,
    *,
    error: str | None = None,
    progress: float | None = None,
    event_message: str | None = None,
    strict: bool = False,
) -> bool:
    value = status.value if isinstance(status, JobStatus) else str(status)
    async with SessionLocal() as session:
        job = await session.get(DownloadJob, job_id)
        if job is None:
            return False
        if strict and not can_transition(job.status, value):
            raise ValueError(f"Invalid job transition: {job.status} -> {value}")
        job.status = value
        if error is not None:
            job.error = error[:2000]
        if progress is not None:
            job.progress = max(0.0, min(100.0, float(progress)))
        if value == JobStatus.COMPLETED.value:
            job.completed_at = datetime.now(UTC)
        session.add(JobEvent(job_id=job_id, event_type=value[:64], message=event_message[:2000] if event_message else None))
        await session.commit()
        return True


async def count_user_running_jobs(user_id: int, *, exclude_job_id: int | None = None) -> int:
    conditions = [DownloadJob.user_id == user_id, DownloadJob.status.in_(RUNNING_STATUSES)]
    if exclude_job_id is not None:
        conditions.append(DownloadJob.id != exclude_job_id)
    async with SessionLocal() as session:
        value = await session.scalar(select(func.count()).select_from(DownloadJob).where(*conditions))
        return int(value or 0)


async def is_job_cancelled(job_id: int) -> bool:
    async with SessionLocal() as session:
        status = await session.scalar(select(DownloadJob.status).where(DownloadJob.id == job_id))
        return status == JobStatus.CANCELLED.value
