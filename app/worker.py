from __future__ import annotations

import asyncio
import socket
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.types import FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import select

from app.bot.client import create_bot
from app.config import get_settings
from app.db import DownloadJob, JobStatus, MediaMetadata, SessionLocal, User, WorkerNode
from app.errors import CancelledError, ErrorCode, classify_error, retry_delay
from app.jobs import is_job_cancelled, record_job_event, set_job_status
from app.progress import ProgressSnapshot
from app.security import redact_secrets
from app.services.downloader import get_downloader_service
from app.services.job_service import deserialize_qualities
from app.services.media import cut_media, fit_media_for_upload

settings = get_settings()
downloader = get_downloader_service()
_cancel_events: dict[int, threading.Event] = {}


def request_cancel(job_id: int) -> None:
    _cancel_events.setdefault(job_id, threading.Event()).set()
    downloader.cancel(str(job_id))


def _cancel_markup(job_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="✖️ Cancel / إلغاء", callback_data=f"cancel:{job_id}")]]
    )


def _retry_markup(job_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="🔁 Retry / إعادة", callback_data=f"retry:{job_id}")]]
    )


async def _job_delivery(job_id: int) -> tuple[int, int | None]:
    async with SessionLocal() as session:
        job = await session.get(DownloadJob, job_id)
        if job is None:
            raise LookupError("Job not found")
        return job.chat_id, job.progress_message_id


async def _edit_status(job_id: int, text: str, *, markup: InlineKeyboardMarkup | None = None) -> None:
    chat_id, message_id = await _job_delivery(job_id)
    if chat_id == 0 or not message_id:
        return
    bot = create_bot()
    try:
        try:
            await bot.edit_message_text(text, chat_id=chat_id, message_id=message_id, reply_markup=markup)
            return
        except TelegramBadRequest as exc:
            if "message is not modified" in str(exc).lower():
                return
            # Metadata messages can be photos; edit their caption instead.
            try:
                await bot.edit_message_caption(
                    chat_id=chat_id,
                    message_id=message_id,
                    caption=text[:1024],
                    reply_markup=markup,
                )
                return
            except TelegramBadRequest:
                return
        except TelegramRetryAfter as exc:
            await asyncio.sleep(min(float(exc.retry_after), 5.0))
    finally:
        await bot.session.close()


async def _is_cancelled(job_id: int, event: threading.Event) -> bool:
    return event.is_set() or await is_job_cancelled(job_id)


async def _update_worker(active_delta: int) -> None:
    worker_id = f"embedded:{socket.gethostname()}"
    async with SessionLocal() as session:
        node = await session.get(WorkerNode, worker_id)
        if node is None:
            node = WorkerNode(id=worker_id, hostname=socket.gethostname(), active_jobs=0)
            session.add(node)
        node.status = "ONLINE"
        node.active_jobs = max(0, int(node.active_jobs or 0) + active_delta)
        node.last_seen = datetime.now(UTC)
        await session.commit()


async def _persist_progress(job_id: int, snapshot: ProgressSnapshot, quality: str) -> None:
    async with SessionLocal() as session:
        job = await session.get(DownloadJob, job_id)
        if job is None or job.status == JobStatus.CANCELLED.value:
            return
        job.progress = snapshot.percent
        job.speed = f"{snapshot.speed:.0f} B/s" if snapshot.speed else None
        job.eta = str(int(snapshot.eta)) if snapshot.eta is not None else None
        await session.commit()
    await _edit_status(
        job_id,
        snapshot.render(job_id=job_id, status=JobStatus.DOWNLOADING.value, quality=quality),
        markup=_cancel_markup(job_id),
    )


async def _download_with_retries(
    job_id: int,
    url: str,
    quality: str,
    job_dir: Path,
    known_qualities: list[int],
    progress_hook,
    cancel_event: threading.Event,
) -> Path:
    for attempt in range(settings.job_max_retries + 1):
        if await _is_cancelled(job_id, cancel_event):
            raise CancelledError("Job cancelled")
        if attempt:
            await set_job_status(job_id, JobStatus.DOWNLOADING, event_message=f"retry attempt={attempt}")
        try:
            return await asyncio.to_thread(
                downloader.download,
                url,
                quality,
                job_dir,
                job_key=str(job_id),
                progress_hook=progress_hook,
                known_qualities=known_qualities,
            )
        except Exception as exc:
            if await _is_cancelled(job_id, cancel_event):
                raise CancelledError("Job cancelled") from exc
            info = classify_error(exc)
            if not info.retryable or attempt >= settings.job_max_retries:
                raise
            delay = retry_delay(
                attempt,
                base=settings.job_retry_base_seconds,
                cap=settings.job_retry_cap_seconds,
            )
            await set_job_status(
                job_id,
                JobStatus.RETRYING,
                event_message=f"{info.code.value}; attempt={attempt + 1}; delay={delay:.2f}",
            )
            await _edit_status(
                job_id,
                f"Job #{job_id}\nStatus: RETRYING\nReason: {info.code.value}\nRetry in: {delay:.1f}s",
                markup=_cancel_markup(job_id),
            )
            await asyncio.sleep(delay)
    raise RuntimeError("Retry loop exhausted")


async def _upload_once(job_id: int, output: Path) -> None:
    chat_id, _ = await _job_delivery(job_id)
    if chat_id == 0:
        return
    bot = create_bot()
    try:
        media = FSInputFile(output)
        caption = f"✅ {output.stem[:900]}"
        suffix = output.suffix.lower()
        if suffix == ".mp4":
            await bot.send_video(chat_id=chat_id, video=media, caption=caption, supports_streaming=True, request_timeout=600)
        elif suffix == ".mp3":
            await bot.send_audio(chat_id=chat_id, audio=media, caption=caption, request_timeout=600)
        else:
            await bot.send_document(chat_id=chat_id, document=media, caption=caption, request_timeout=600)
    finally:
        await bot.session.close()


async def _upload_with_retries(job_id: int, output: Path, cancel_event: threading.Event) -> None:
    for attempt in range(settings.job_max_retries + 1):
        if await _is_cancelled(job_id, cancel_event):
            raise CancelledError("Job cancelled")
        try:
            await _upload_once(job_id, output)
            return
        except Exception as exc:
            info = classify_error(exc)
            if not info.retryable or attempt >= settings.job_max_retries:
                raise
            delay = retry_delay(attempt, base=settings.job_retry_base_seconds, cap=settings.job_retry_cap_seconds)
            await set_job_status(job_id, JobStatus.RETRYING, event_message=f"upload {info.code.value}; delay={delay:.2f}")
            await asyncio.sleep(delay)
            await set_job_status(job_id, JobStatus.UPLOADING, event_message="upload retry")


async def process_download(job_id: int) -> None:
    cancel_event = _cancel_events.setdefault(job_id, threading.Event())
    await _update_worker(1)
    try:
        async with SessionLocal() as session:
            result = await session.execute(
                select(DownloadJob, User, MediaMetadata)
                .join(User, DownloadJob.user_id == User.id)
                .outerjoin(MediaMetadata, MediaMetadata.job_id == DownloadJob.id)
                .where(DownloadJob.id == job_id)
            )
            row = result.first()
            if row is None:
                return
            job, _user, metadata = row
            if job.status == JobStatus.CANCELLED.value:
                return
            if job.status not in {JobStatus.QUEUED.value, JobStatus.RETRYING.value}:
                return
            job.status = JobStatus.DOWNLOADING.value
            job.worker_id = f"embedded:{socket.gethostname()}"
            job.error = None
            job.progress = 0.0
            await session.commit()
            source_url = job.source_url
            quality = job.selected_quality or "best"
            cut_start = job.cut_start
            cut_end = job.cut_end
            source_duration = job.duration
            chat_id = job.chat_id
            known_qualities = deserialize_qualities(metadata.formats_json if metadata else "[]")
            cut_mode = (metadata.cut_mode if metadata else "PRECISE") or "PRECISE"

        await record_job_event(job_id, JobStatus.DOWNLOADING.value, f"quality={quality}")
        job_dir = settings.download_dir / str(job_id)
        loop = asyncio.get_running_loop()
        last_update = 0.0

        def consume_future(future) -> None:
            try:
                future.result()
            except Exception:
                pass

        def progress_hook(payload: dict) -> None:
            nonlocal last_update
            if cancel_event.is_set():
                raise CancelledError("Job cancelled")
            status = payload.get("status")
            if status == "finished":
                future = asyncio.run_coroutine_threadsafe(
                    set_job_status(job_id, JobStatus.MERGING, event_message="download streams finished"),
                    loop,
                )
                future.add_done_callback(consume_future)
                return
            if status != "downloading":
                return
            now = time.monotonic()
            if now - last_update < settings.progress_update_seconds:
                return
            last_update = now
            snapshot = ProgressSnapshot.from_ytdlp(payload)
            future = asyncio.run_coroutine_threadsafe(_persist_progress(job_id, snapshot, quality), loop)
            future.add_done_callback(consume_future)

        output = await _download_with_retries(
            job_id,
            source_url,
            quality,
            job_dir,
            known_qualities,
            progress_hook,
            cancel_event,
        )
        if await _is_cancelled(job_id, cancel_event):
            raise CancelledError("Job cancelled")

        if cut_start is not None and cut_end is not None:
            await set_job_status(job_id, JobStatus.CUTTING, event_message=f"mode={cut_mode}; {cut_start}-{cut_end}")
            await _edit_status(
                job_id,
                f"Job #{job_id}\nStatus: CUTTING\nMode: {cut_mode}\nRange: {cut_start:.2f}s → {cut_end:.2f}s",
                markup=_cancel_markup(job_id),
            )
            output = await cut_media(
                output,
                cut_start,
                cut_end,
                mode=cut_mode,
                source_duration=float(source_duration) if source_duration else None,
                cancel_event=cancel_event,
            )

        if output.stat().st_size > settings.max_file_size_bytes:
            raise RuntimeError("File too large: output exceeds MAX_FILE_SIZE_MB")

        if chat_id != 0 and output.stat().st_size > settings.telegram_upload_limit_bytes:
            await set_job_status(job_id, JobStatus.PROCESSING, event_message="adaptive Telegram size fit")
            output = await fit_media_for_upload(
                output,
                settings.telegram_upload_limit_bytes,
                job_dir,
                attempts=2,
                cancel_event=cancel_event,
            )

        if await _is_cancelled(job_id, cancel_event):
            raise CancelledError("Job cancelled")

        file_size = output.stat().st_size
        async with SessionLocal() as session:
            db_job = await session.get(DownloadJob, job_id)
            if db_job is None:
                raise LookupError("Job not found")
            if db_job.status == JobStatus.CANCELLED.value:
                raise CancelledError("Job cancelled")
            db_job.output_path = str(output)
            db_job.file_size = file_size
            db_job.progress = 100.0
            db_job.status = JobStatus.UPLOADING.value if chat_id != 0 else JobStatus.COMPLETED.value
            if chat_id == 0:
                db_job.completed_at = datetime.now(UTC)
            await session.commit()

        if chat_id != 0:
            await record_job_event(job_id, JobStatus.UPLOADING.value, f"bytes={file_size}")
            await _edit_status(
                job_id,
                f"Job #{job_id}\nStatus: UPLOADING\nQuality: {quality}\nProgress: 100%",
                markup=_cancel_markup(job_id),
            )
            await _upload_with_retries(job_id, output, cancel_event)
            await set_job_status(job_id, JobStatus.COMPLETED, progress=100.0)
        else:
            await record_job_event(job_id, JobStatus.COMPLETED.value, f"dashboard file ready; bytes={file_size}")

        await _edit_status(job_id, f"Job #{job_id}\nStatus: COMPLETED ✅\nQuality: {quality}")
    except Exception as exc:
        info = classify_error(exc)
        if info.code == ErrorCode.CANCELLED or cancel_event.is_set():
            await set_job_status(job_id, JobStatus.CANCELLED, error=ErrorCode.CANCELLED.value, event_message="cancelled")
            await _edit_status(job_id, f"Job #{job_id}\nStatus: CANCELLED")
        else:
            safe_message = redact_secrets(
                str(exc), bot_token=settings.bot_token, database_url=settings.database_url
            )[:1500]
            await set_job_status(
                job_id,
                JobStatus.FAILED,
                error=f"{info.code.value}: {safe_message}",
                event_message=info.code.value,
            )
            await _edit_status(
                job_id,
                f"Job #{job_id}\nStatus: FAILED ❌\nError: {info.code.value}",
                markup=_retry_markup(job_id),
            )
    finally:
        _cancel_events.pop(job_id, None)
        downloader.forget(str(job_id))
        await _update_worker(-1)
