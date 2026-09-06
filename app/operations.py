from __future__ import annotations

import shutil
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update

from app.db import DownloadJob, JobEvent, JobStatus, SessionLocal, WorkerNode

UNSAFE_INTERRUPTED = {
    JobStatus.ANALYZING.value,
    JobStatus.PROBING.value,
    JobStatus.RETRYING.value,
    JobStatus.DOWNLOADING.value,
    JobStatus.MERGING.value,
    JobStatus.PROCESSING.value,
    JobStatus.CUTTING.value,
    JobStatus.UPLOADING.value,
}


@dataclass(frozen=True, slots=True)
class ReconciliationResult:
    requeue_ids: tuple[int, ...]
    failed_ids: tuple[int, ...]


async def reconcile_stale_jobs() -> ReconciliationResult:
    """Requeue durable QUEUED rows; fail process-bound stages that cannot safely resume."""
    requeue: list[int] = []
    failed: list[int] = []
    async with SessionLocal() as session:
        jobs = list(await session.scalars(select(DownloadJob).where(DownloadJob.status.in_(UNSAFE_INTERRUPTED | {JobStatus.QUEUED.value}))))
        for job in jobs:
            if job.status == JobStatus.QUEUED.value:
                requeue.append(job.id)
                job.worker_id = None
                continue
            previous = job.status
            job.status = JobStatus.FAILED.value
            job.error = "INTERRUPTED: service restarted while this stage was process-bound"
            job.worker_id = None
            failed.append(job.id)
            session.add(JobEvent(job_id=job.id, event_type="INTERRUPTED", message=f"recovered from stale status={previous}"))
        if jobs:
            await session.commit()
    return ReconciliationResult(tuple(requeue), tuple(failed))


async def fail_interrupted_inline_jobs() -> list[int]:
    """Backward-compatible alias used by older callers/tests."""
    result = await reconcile_stale_jobs()
    return list(result.failed_ids)


async def mark_stale_workers_offline(max_age_seconds: int = 60) -> int:
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
    return {
        "ok": database_ok and ffmpeg_ok and ffprobe_ok,
        "database": database_ok,
        "database_error": database_error,
        "ffmpeg": ffmpeg_ok,
        "ffprobe": ffprobe_ok,
    }
