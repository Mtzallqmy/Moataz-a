from __future__ import annotations

import asyncio

from arq import create_pool
from arq.connections import RedisSettings

from app.config import get_settings

settings = get_settings()
_background_tasks: set[asyncio.Task] = set()
_inline_semaphore = asyncio.Semaphore(settings.max_concurrent_jobs)


def redis_settings() -> RedisSettings:
    # Keep a valid value for optional ARQ worker imports; Redis is not contacted unless used.
    return RedisSettings.from_dsn(settings.redis_url or "redis://localhost:6379/0")


async def _run_inline(job_id: int) -> None:
    # Import lazily to avoid a module-level circular import: app.worker imports redis_settings.
    from app.worker import process_download

    async with _inline_semaphore:
        worker_id = settings.worker_name.strip() or "embedded-worker"
        await process_download({"worker_id": worker_id}, job_id)


def _task_done(task: asyncio.Task) -> None:
    _background_tasks.discard(task)
    # Retrieve the result so exceptions do not become "Task exception was never retrieved".
    try:
        task.result()
    except asyncio.CancelledError:
        pass
    except Exception:
        # process_download already records the user-facing/database error.
        pass


async def enqueue_download(job_id: int) -> None:
    if settings.queue_backend == "redis" and settings.redis_url:
        redis = await create_pool(redis_settings())
        try:
            await redis.enqueue_job("process_download", job_id)
        finally:
            await redis.aclose()
        return

    # Default deployment path: no Redis service or REDIS_URL is required.
    task = asyncio.create_task(_run_inline(job_id), name=f"download-job-{job_id}")
    _background_tasks.add(task)
    task.add_done_callback(_task_done)
