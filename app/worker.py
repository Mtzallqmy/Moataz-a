from __future__ import annotations

import asyncio
import os
import shutil
import socket
import time
from datetime import UTC, datetime

from aiogram.types import FSInputFile
from sqlalchemy import func, select

from app.bot.client import create_bot
from app.config import get_settings
from app.db import DownloadJob, JobStatus, SessionLocal, User, WorkerNode, init_db
from app.i18n import tr
from app.queue import redis_settings
from app.services.downloader import download_media
from app.services.media import cut_media
from app.utils import progress_bar

settings = get_settings()
ACTIVE_STATUSES = [
    JobStatus.QUEUED.value,
    JobStatus.DOWNLOADING.value,
    JobStatus.PROCESSING.value,
    JobStatus.UPLOADING.value,
]


def _human_bytes_per_second(value: float | int | None) -> str:
    if not value:
        return "—"
    size = float(value)
    for unit in ("B/s", "KB/s", "MB/s", "GB/s"):
        if size < 1024 or unit == "GB/s":
            return f"{size:.1f} {unit}"
        size /= 1024
    return "—"


def _human_eta(value: int | float | None) -> str:
    if value is None:
        return "—"
    seconds = max(0, int(value))
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
    return f"{minutes:02d}:{seconds:02d}"


async def _edit_status(job_id: int, text: str) -> None:
    async with SessionLocal() as session:
        job = await session.get(DownloadJob, job_id)
        if not job or not job.progress_message_id:
            return
        chat_id = job.chat_id
        message_id = job.progress_message_id
    bot = create_bot()
    try:
        await bot.edit_message_text(text, chat_id=chat_id, message_id=message_id)
    except Exception:
        pass
    finally:
        await bot.session.close()


async def _get_chat_id(job_id: int) -> int:
    async with SessionLocal() as session:
        job = await session.get(DownloadJob, job_id)
        if not job:
            raise RuntimeError("Job not found")
        return job.chat_id


async def _heartbeat_once(worker_id: str, hostname: str, status: str = "ONLINE") -> None:
    async with SessionLocal() as session:
        active_jobs = await session.scalar(
            select(func.count())
            .select_from(DownloadJob)
            .where(DownloadJob.worker_id == worker_id, DownloadJob.status.in_(ACTIVE_STATUSES))
        ) or 0
        node = await session.get(WorkerNode, worker_id)
        if node is None:
            node = WorkerNode(id=worker_id, hostname=hostname)
            session.add(node)
        node.hostname = hostname
        node.status = status
        node.active_jobs = active_jobs if status == "ONLINE" else 0
        node.last_seen = datetime.now(UTC)
        await session.commit()


async def _heartbeat_loop(worker_id: str, hostname: str) -> None:
    while True:
        await _heartbeat_once(worker_id, hostname)
        await asyncio.sleep(15)


async def _cleanup_loop() -> None:
    """Delete non-active temporary job directories older than six hours."""
    while True:
        try:
            async with SessionLocal() as session:
                active_ids = set(
                    await session.scalars(
                        select(DownloadJob.id).where(DownloadJob.status.in_(ACTIVE_STATUSES))
                    )
                )
            cutoff = time.time() - 6 * 60 * 60
            for path in settings.download_dir.iterdir():
                if not path.is_dir() or not path.name.isdigit():
                    continue
                if int(path.name) in active_ids:
                    continue
                if path.stat().st_mtime < cutoff:
                    shutil.rmtree(path, ignore_errors=True)
        except Exception:
            pass
        await asyncio.sleep(30 * 60)


async def process_download(ctx, job_id: int) -> None:
    worker_id = ctx["worker_id"]
    async with SessionLocal() as session:
        result = await session.execute(
            select(DownloadJob, User)
            .join(User, DownloadJob.user_id == User.id)
            .where(DownloadJob.id == job_id)
        )
        row = result.first()
        if not row:
            return
        job, user = row
        language = user.language
        job.status = JobStatus.DOWNLOADING.value
        job.worker_id = worker_id
        job.error = None
        await session.commit()
        source_url = job.source_url
        quality = job.selected_quality or "best"
        cut_start = job.cut_start
        cut_end = job.cut_end

    job_dir = settings.download_dir / str(job_id)
    loop = asyncio.get_running_loop()
    last_update = 0.0

    async def save_progress(payload: dict) -> None:
        nonlocal last_update
        now = time.monotonic()
        if now - last_update < settings.progress_update_seconds:
            return
        last_update = now
        downloaded = payload.get("downloaded_bytes") or 0
        total = payload.get("total_bytes") or payload.get("total_bytes_estimate") or 0
        percentage = (downloaded / total * 100) if total else 0.0
        speed = _human_bytes_per_second(payload.get("speed"))
        eta = _human_eta(payload.get("eta"))
        async with SessionLocal() as session:
            db_job = await session.get(DownloadJob, job_id)
            if not db_job:
                return
            db_job.progress = percentage
            db_job.speed = speed
            db_job.eta = eta
            await session.commit()
        await _edit_status(
            job_id,
            tr(
                language,
                "download",
                progress=percentage,
                bar=progress_bar(percentage),
                speed=speed,
                eta=eta,
            ),
        )

    def progress_hook(payload: dict) -> None:
        if payload.get("status") == "downloading":
            asyncio.run_coroutine_threadsafe(save_progress(payload), loop)

    try:
        output = await asyncio.to_thread(download_media, source_url, quality, job_dir, progress_hook)

        if cut_start is not None and cut_end is not None:
            async with SessionLocal() as session:
                db_job = await session.get(DownloadJob, job_id)
                db_job.status = JobStatus.PROCESSING.value
                await session.commit()
            await _edit_status(job_id, tr(language, "processing"))
            output = await cut_media(output, cut_start, cut_end)

        file_size = output.stat().st_size
        if file_size > settings.max_file_size_bytes:
            raise RuntimeError(
                f"Output is {file_size / 1024 / 1024:.1f} MB, above MAX_FILE_SIZE_MB"
            )
        if file_size > 49 * 1024 * 1024:
            raise RuntimeError(
                "File exceeds the conservative official Telegram Bot API upload limit. "
                "Choose a smaller quality or clip."
            )

        async with SessionLocal() as session:
            db_job = await session.get(DownloadJob, job_id)
            db_job.status = JobStatus.UPLOADING.value
            db_job.output_path = str(output)
            db_job.file_size = file_size
            db_job.progress = 100.0
            await session.commit()
        await _edit_status(job_id, tr(language, "uploading"))

        chat_id = await _get_chat_id(job_id)
        bot = create_bot()
        try:
            media = FSInputFile(output)
            caption = f"✅ {output.stem[:900]}"
            if output.suffix.lower() == ".mp4":
                await bot.send_video(
                    chat_id=chat_id,
                    video=media,
                    caption=caption,
                    supports_streaming=True,
                    request_timeout=600,
                )
            elif output.suffix.lower() == ".mp3":
                await bot.send_audio(
                    chat_id=chat_id,
                    audio=media,
                    caption=caption,
                    request_timeout=600,
                )
            else:
                await bot.send_document(
                    chat_id=chat_id,
                    document=media,
                    caption=caption,
                    request_timeout=600,
                )
        finally:
            await bot.session.close()

        async with SessionLocal() as session:
            db_job = await session.get(DownloadJob, job_id)
            db_job.status = JobStatus.COMPLETED.value
            db_job.completed_at = datetime.now(UTC)
            await session.commit()
        await _edit_status(job_id, tr(language, "done"))
        shutil.rmtree(job_dir, ignore_errors=True)
    except Exception as exc:
        async with SessionLocal() as session:
            db_job = await session.get(DownloadJob, job_id)
            if db_job:
                db_job.status = JobStatus.FAILED.value
                db_job.error = str(exc)[:2000]
                await session.commit()
        await _edit_status(job_id, tr(language, "failed", error=str(exc)[:800]))
        raise


async def startup(ctx) -> None:
    await init_db()
    hostname = socket.gethostname()
    worker_id = settings.worker_name.strip() or f"{hostname}-{os.getpid()}"
    ctx["worker_id"] = worker_id
    ctx["hostname"] = hostname
    await _heartbeat_once(worker_id, hostname)
    ctx["heartbeat_task"] = asyncio.create_task(_heartbeat_loop(worker_id, hostname))
    ctx["cleanup_task"] = asyncio.create_task(_cleanup_loop())


async def shutdown(ctx) -> None:
    for key in ("heartbeat_task", "cleanup_task"):
        task = ctx.get(key)
        if task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
    if ctx.get("worker_id"):
        await _heartbeat_once(ctx["worker_id"], ctx.get("hostname", "unknown"), status="OFFLINE")


class WorkerSettings:
    functions = [process_download]
    on_startup = startup
    on_shutdown = shutdown
    redis_settings = redis_settings()
    max_jobs = settings.max_concurrent_jobs
    job_timeout = 60 * 60 * 4
    keep_result = 3600
    max_tries = 1
