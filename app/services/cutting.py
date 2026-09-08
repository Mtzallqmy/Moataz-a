from __future__ import annotations

from dataclasses import dataclass

from app.db import DownloadJob, JobStatus, MediaMetadata, SessionLocal
from app.services.job_service import queue_existing_job


@dataclass(frozen=True, slots=True)
class CutPlan:
    start: float
    end: float
    mode: str
    preset: str

    @property
    def duration(self) -> float:
        return self.end - self.start


def resolve_cut_plan(
    *,
    start: float,
    source_duration: float | None,
    preset: str = "free",
    end: float | None = None,
    mode: str = "PRECISE",
) -> CutPlan:
    cut_mode = mode.upper().strip()
    if cut_mode not in {"FAST", "PRECISE"}:
        raise ValueError("Cut mode must be FAST or PRECISE")

    normalized_preset = preset.lower().strip()
    if normalized_preset not in {"free", "30", "60"}:
        raise ValueError("Cut preset must be free, 30 or 60")

    start_value = float(start)
    if start_value < 0:
        raise ValueError("Cut start cannot be negative")

    if normalized_preset == "free":
        if end is None:
            raise ValueError("Free cut requires an end time")
        end_value = float(end)
    else:
        end_value = start_value + float(normalized_preset)

    if end_value <= start_value:
        raise ValueError("Cut end must be after the start")
    if source_duration is not None and end_value > float(source_duration) + 0.05:
        if normalized_preset in {"30", "60"}:
            raise ValueError(
                f"Not enough media remains after {start_value:g}s for an exact {normalized_preset}-second clip"
            )
        raise ValueError("Cut end exceeds media duration")

    return CutPlan(start=start_value, end=end_value, mode=cut_mode, preset=normalized_preset)


async def configure_and_queue_cut(
    job_id: int,
    *,
    start: float,
    preset: str = "free",
    end: float | None = None,
    mode: str = "PRECISE",
    quality: str = "best",
) -> CutPlan:
    async with SessionLocal() as session:
        job = await session.get(DownloadJob, job_id)
        if job is None:
            raise LookupError("Job not found")
        if job.status != JobStatus.READY.value:
            raise ValueError("Job is not READY for cutting")
        metadata = await session.get(MediaMetadata, job_id)
        plan = resolve_cut_plan(
            start=start,
            end=end,
            preset=preset,
            mode=mode,
            source_duration=float(job.duration) if job.duration is not None else None,
        )
        job.cut_start = plan.start
        job.cut_end = plan.end
        if metadata is not None:
            metadata.cut_mode = plan.mode
        await session.commit()

    await queue_existing_job(job_id, quality)
    return plan
