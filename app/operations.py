from __future__ import annotations

import shutil
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update

from app.db import DownloadJob, JobEvent, JobStatus, SessionLocal, WorkerNode


INTERRUPTED_INLINE_STATUSES = {
    JobStatus.PENDING.value,
    JobStatus.ANALYZING.value,
    JobStatus.QUEUED.value,
    JobStatus.RETRYING.value,
    JobStatus.DOWNLOADING.value,
    JobStatus.MERGING.value,
    JobStatus.PROCESSING.value,
    JobStatus.CUTTING.value,
    JobStatus.UPLOADING.value,
}


async def fail_interrupted_inline_jobs() -> list[int]:
    """Close jobs that cannot survive an embedded-worker process restart.

    Inline tasks live in process memory. After a restart there is no safe task to resume,
    so preserving a RUNNING status would leave the user with a permanently stuck job.
    Failed jobs remain manually retryable through the existing retry flow.
    """

    interrupted_ids: list[int] = []
    async with SessionLocal() as session:
        jobs = list(
            await session.scalars(
                select(DownloadJob).where(
                    DownloadJob.status.in_(sorted(INTERRUPTED_INLINE_STATUSES))
                )
            )
        )
        for job in jobs:
            interrupted_ids.append(job.id)
            previous = job.status
            job.status = JobStatus.FAILED.value
            job.error = "INTERRUPTED: application restarted while the inline job was active"
            job.worker_id = None
            session.add(
                JobEvent(
                    job_id=job.id,
                    event_type="INTERRUPTED",
                    message=f"recovered from stale status={previous}",
                )
            )
        if interrupted_ids:
            await session.commit()
    return interrupted_ids


async def mark_stale_workers_offline(max_age_seconds: int = 60) -> int:
    """Mark worker heartbeats offline after a bounded grace period."""

    cutoff = datetime.now(UTC) - timedelta(seconds=max(15, int(max_age_seconds)))
    async with SessionLocal() as session:
        result = await session.execute(
            update(WorkerNode)
            .where(WorkerNode.last_seen < cutoff, WorkerNode.status != "OFFLINE")
            .values(status="OFFLINE", active_jobs=0)
        )
        await session.commit()
        return int(result.rowcount or 0)


async def readiness_snapshot() -> dict[str, object]:
    """Return dependency readiness without contacting Telegram or external media sites."""

    database_ok = False
    database_error: str | None = None
    try:
        async with SessionLocal() as session:
            await session.execute(select(1))
        database_ok = True
    except Exception as exc:
        database_error = type(exc).__name__

    ffmpeg_ok = shutil.which("ffmpeg") is not None
    ffprobe_ok = shutil.which("ffprobe") is not None
    ok = database_ok and ffmpeg_ok and ffprobe_ok
    return {
        "ok": ok,
        "database": database_ok,
        "database_error": database_error,
        "ffmpeg": ffmpeg_ok,
        "ffprobe": ffprobe_ok,
    }
