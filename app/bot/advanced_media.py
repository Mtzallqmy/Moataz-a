from __future__ import annotations

import asyncio
import math
import time
from dataclasses import dataclass

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import select

import app.bot.ai as ai_module
import app.bot.features as feature_module
import app.bot.handlers as handlers_module
import app.worker as worker_module
from app.bot.access import ensure_user, is_allowed
from app.config import get_settings
from app.db import DownloadJob, JobStatus, MediaMetadata, SessionLocal, User
from app.errors import CancelledError, ErrorCode, classify_error, user_error_message
from app.jobs import record_job_event, set_job_status
from app.progress import ProgressSnapshot
from app.security import canonicalize_url, redact_secrets
from app.services.downloader import MediaInfo
from app.services.job_service import deserialize_formats, deserialize_qualities, queue_existing_job
from app.services.segmenting import MAX_SPLIT_SEGMENTS, split_media
from app.services.urls import parse_bulk_urls
from app.utils import parse_time, seconds_to_hms

settings = get_settings()
router = Router(name="advanced-media")


class AdvancedMediaState(StatesGroup):
    waiting_split_range = State()


@dataclass(frozen=True, slots=True)
class SavedMedia:
    job_id: int
    title: str
    platform: str


def public_menu_keyboard(language: str = "ar") -> InlineKeyboardMarkup:
    if language == "en":
        labels = {
            "video": "🎬 Video",
            "audio": "🎵 MP3",
            "cut": "✂️ Cut / split",
            "history": "📋 My downloads",
            "bulk": "📥 Multiple URLs",
            "saved": "🔖 Saved / recent links",
            "ai": "🤖 AI Chat",
            "lang": "🌐 Language",
            "help": "ℹ️ Help",
            "studio": "🎞 Media Studio",
        }
    else:
        labels = {
            "video": "🎬 تحميل فيديو",
            "audio": "🎵 MP3",
            "cut": "✂️ قص وتقسيم",
            "history": "📋 تحميلاتي",
            "bulk": "📥 تحميل عدة روابط",
            "saved": "🔖 الروابط المحفوظة",
            "ai": "🤖 دردشة AI",
            "lang": "🌐 اللغة",
            "help": "ℹ️ مساعدة",
            "studio": "🎞 مشروع مونتاج",
        }
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=labels["video"], callback_data="menu:video"),
                InlineKeyboardButton(text=labels["audio"], callback_data="menu:audio"),
            ],
            [
                InlineKeyboardButton(text=labels["cut"], callback_data="menu:cut"),
                InlineKeyboardButton(text=labels["history"], callback_data="history:0"),
            ],
            [
                InlineKeyboardButton(text=labels["bulk"], callback_data="menu:bulk"),
                InlineKeyboardButton(text=labels["saved"], callback_data="menu:saved"),
            ],
            [InlineKeyboardButton(text=labels["ai"], callback_data="menu:ai")],
            [InlineKeyboardButton(text=labels["studio"], callback_data="menu:studio")],
            [
                InlineKeyboardButton(text=labels["lang"], callback_data="menu:lang"),
                InlineKeyboardButton(text=labels["help"], callback_data="menu:help"),
            ],
        ]
    )


def advanced_cut_menu_keyboard(job_id: int, mode: str = "PRECISE") -> InlineKeyboardMarkup:
    normalized_mode = "FAST" if mode.upper() == "FAST" else "PRECISE"
    other_mode = "PRECISE" if normalized_mode == "FAST" else "FAST"
    other_label = "🎯 PRECISE" if other_mode == "PRECISE" else "⚡ FAST"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✂️ قص حر", callback_data=f"cutpick:{job_id}:free:{normalized_mode}")],
            [
                InlineKeyboardButton(text="📱 مقطع 30ث", callback_data=f"cutpick:{job_id}:30:{normalized_mode}"),
                InlineKeyboardButton(text="🎞️ مقطع 60ث", callback_data=f"cutpick:{job_id}:60:{normalized_mode}"),
            ],
            [
                InlineKeyboardButton(text="🧩 تقسيم مستمر 30ث", callback_data=f"advsplit:{job_id}:30"),
                InlineKeyboardButton(text="🧩 تقسيم مستمر 60ث", callback_data=f"advsplit:{job_id}:60"),
            ],
            [
                InlineKeyboardButton(text=f"الوضع: {normalized_mode}", callback_data=f"cutmenu:{job_id}:{normalized_mode}"),
                InlineKeyboardButton(text=other_label, callback_data=f"cutmenu:{job_id}:{other_mode}"),
            ],
            [InlineKeyboardButton(text="♻️ استخدام الرابط مجددًا", callback_data=f"reusemenu:{job_id}")],
            [InlineKeyboardButton(text="⬅️ خيارات التنزيل", callback_data=f"cutback:{job_id}")],
        ]
    )


def split_choice_keyboard(job_id: int, segment_seconds: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"🎬 كامل {segment_seconds}ث فيديو",
                    callback_data=f"splitfull:{job_id}:{segment_seconds}:best",
                ),
                InlineKeyboardButton(
                    text=f"🎵 كامل {segment_seconds}ث صوت",
                    callback_data=f"splitfull:{job_id}:{segment_seconds}:audio",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="🎛 نطاق مخصص فيديو",
                    callback_data=f"splitrange:{job_id}:{segment_seconds}:best",
                ),
                InlineKeyboardButton(
                    text="🎛 نطاق مخصص صوت",
                    callback_data=f"splitrange:{job_id}:{segment_seconds}:audio",
                ),
            ],
            [InlineKeyboardButton(text="⬅️ رجوع للقص", callback_data=f"cutmenu:{job_id}:PRECISE")],
        ]
    )


def reuse_menu_keyboard(job_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🎬 فيديو مجددًا", callback_data=f"reuse:{job_id}:video"),
                InlineKeyboardButton(text="🎵 MP3 مجددًا", callback_data=f"reuse:{job_id}:audio"),
            ],
            [InlineKeyboardButton(text="✂️ قص/تقسيم جديد", callback_data=f"reuse:{job_id}:cut")],
            [InlineKeyboardButton(text="🧩 تقسيم مستمر", callback_data=f"reuse:{job_id}:split")],
            [InlineKeyboardButton(text="🔖 بقية الروابط", callback_data="menu:saved")],
            [InlineKeyboardButton(text="🏠 الرئيسية", callback_data="menu:home")],
        ]
    )


def completed_reuse_keyboard(job_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="♻️ استخدام نفس الرابط", callback_data=f"reusemenu:{job_id}")],
            [
                InlineKeyboardButton(text="🔖 الروابط", callback_data="menu:saved"),
                InlineKeyboardButton(text="🏠 الرئيسية", callback_data="menu:home"),
            ],
        ]
    )


async def _callback_user(callback: CallbackQuery) -> User | None:
    if not callback.from_user or not await is_allowed(callback.from_user.id):
        await callback.answer("Unavailable", show_alert=True)
        return None
    return await ensure_user(callback.from_user.id, callback.from_user.username)


async def _owned_job(callback: CallbackQuery, job_id: int) -> tuple[DownloadJob, User] | None:
    if not callback.from_user:
        return None
    async with SessionLocal() as session:
        result = await session.execute(
            select(DownloadJob, User)
            .join(User, DownloadJob.user_id == User.id)
            .where(DownloadJob.id == job_id, User.telegram_id == callback.from_user.id)
        )
        row = result.first()
        if row is None:
            await callback.answer("Job غير موجود", show_alert=True)
            return None
        job, user = row
        session.expunge(job)
        session.expunge(user)
        return job, user


async def _recent_saved(user_id: int, limit: int = 10) -> list[SavedMedia]:
    async with SessionLocal() as session:
        rows = list(
            await session.scalars(
                select(DownloadJob)
                .where(DownloadJob.user_id == user_id, DownloadJob.duration.is_not(None))
                .order_by(DownloadJob.id.desc())
                .limit(60)
            )
        )
    seen: set[str] = set()
    result: list[SavedMedia] = []
    for job in rows:
        try:
            key = canonicalize_url(job.source_url)
        except ValueError:
            key = job.source_url
        if key in seen:
            continue
        seen.add(key)
        result.append(SavedMedia(job.id, (job.title or "Media").replace("\n", " ")[:42], job.platform))
        if len(result) >= limit:
            break
    return result


def _saved_keyboard(items: list[SavedMedia]) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for item in items:
        label = f"♻️ {item.title}"
        if len(label) > 52:
            label = label[:49] + "…"
        rows.append([InlineKeyboardButton(text=label, callback_data=f"reusemenu:{item.job_id}")])
    rows.append([InlineKeyboardButton(text="🏠 الرئيسية", callback_data="menu:home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _clone_ready_job(source_job_id: int, user_id: int, chat_id: int) -> tuple[DownloadJob, MediaInfo]:
    async with SessionLocal() as session:
        result = await session.execute(
            select(DownloadJob, MediaMetadata)
            .outerjoin(MediaMetadata, MediaMetadata.job_id == DownloadJob.id)
            .where(DownloadJob.id == source_job_id, DownloadJob.user_id == user_id)
        )
        row = result.first()
        if row is None:
            raise LookupError("Saved job not found")
        source, metadata = row
        formats_json = metadata.formats_json if metadata else "[]"
        normalized_url = metadata.normalized_url if metadata else canonicalize_url(source.source_url)
        clone = DownloadJob(
            user_id=user_id,
            chat_id=chat_id,
            source_url=source.source_url,
            platform=source.platform,
            title=source.title,
            duration=source.duration,
            thumbnail=source.thumbnail,
            status=JobStatus.READY.value,
        )
        session.add(clone)
        await session.flush()
        session.add(
            MediaMetadata(
                job_id=clone.id,
                uploader=metadata.uploader if metadata else None,
                formats_json=formats_json,
                normalized_url=normalized_url,
                is_playlist=False,
                playlist_count=0,
                cut_mode="PRECISE",
                source="telegram",
                priority=metadata.priority if metadata else 0,
            )
        )
        await session.commit()
        await session.refresh(clone)
        formats = deserialize_formats(formats_json)
        info = MediaInfo(
            title=clone.title or "Media",
            thumbnail=clone.thumbnail,
            duration=clone.duration,
            uploader=metadata.uploader if metadata else None,
            platform=clone.platform,
            formats=formats,
            qualities=sorted({item.height for item in formats}),
            webpage_url=clone.source_url,
        )
        session.expunge(clone)
        return clone, info


async def _configure_split(
    job_id: int,
    *,
    segment_seconds: int,
    quality: str,
    start: float = 0.0,
    end: float | None = None,
) -> tuple[float, float, int]:
    if segment_seconds not in {30, 60}:
        raise ValueError("Unsupported segment size")
    async with SessionLocal() as session:
        job = await session.get(DownloadJob, job_id)
        metadata = await session.get(MediaMetadata, job_id)
        if job is None or metadata is None:
            raise LookupError("Job not found")
        if job.status != JobStatus.READY.value:
            raise ValueError("Job is not READY")
        if job.duration is None or job.duration <= 0:
            raise ValueError("Media duration is unavailable")
        start_value = float(start)
        end_value = float(job.duration if end is None else end)
        if start_value < 0 or end_value <= start_value or end_value > float(job.duration) + 0.05:
            raise ValueError("Invalid split range")
        count = int(math.ceil((end_value - start_value) / segment_seconds))
        if count > MAX_SPLIT_SEGMENTS:
            raise ValueError(
                f"سيتم إنشاء {count} مقطعًا؛ الحد {MAX_SPLIT_SEGMENTS}. استخدم 60 ثانية أو اختر نطاقًا أقصر."
            )
        job.cut_start = start_value
        job.cut_end = end_value
        metadata.cut_mode = f"SPLIT{segment_seconds}"
        await session.commit()
    await queue_existing_job(job_id, quality)
    return start_value, end_value, count


async def _queue_split_feedback(
    callback: CallbackQuery,
    *,
    job_id: int,
    segment_seconds: int,
    quality: str,
    start: float,
    end: float | None,
) -> None:
    user = await _callback_user(callback)
    if user is None:
        return
    try:
        start_value, end_value, count = await _configure_split(
            job_id,
            segment_seconds=segment_seconds,
            quality=quality,
            start=start,
            end=end,
        )
    except (LookupError, ValueError) as exc:
        await callback.answer(str(exc)[:180], show_alert=True)
        return
    if callback.message:
        progress = await callback.message.answer(
            f"🧩 Job #{job_id} • QUEUED\n"
            f"{count} مقطع × {segment_seconds}ث تقريبًا\n"
            f"النطاق: {seconds_to_hms(start_value)} → {seconds_to_hms(end_value)}\n"
            f"النوع: {'MP3' if quality == 'audio' else 'Video'}",
            reply_markup=feature_module._cancel_keyboard(job_id),
        )
        await feature_module._update_progress_message(job_id, progress.message_id)
    await callback.answer()


@router.callback_query(F.data == "menu:saved")
async def saved_links(callback: CallbackQuery) -> None:
    user = await _callback_user(callback)
    if user is None:
        return
    items = await _recent_saved(user.id)
    if callback.message:
        text = (
            "🔖 Saved / recent links\nChoose any link to use it again without typing it."
            if user.language == "en"
            else "🔖 الروابط المحفوظة والأخيرة\nاختر أي رابط لاستخدامه مرة أخرى دون إعادة كتابته."
        )
        if not items:
            text = "No saved links yet." if user.language == "en" else "لا توجد روابط محفوظة حتى الآن."
        await feature_module._edit_callback(callback, text, _saved_keyboard(items))
    await callback.answer()


@router.callback_query(F.data.startswith("reusemenu:"))
async def reuse_menu(callback: CallbackQuery) -> None:
    job_id = int(callback.data.split(":", 1)[1])
    owned = await _owned_job(callback, job_id)
    if owned is None:
        return
    job, user = owned
    text = (
        f"♻️ Reuse link\n{job.title or 'Media'}\nChoose a new operation. A fresh Job will be created."
        if user.language == "en"
        else f"♻️ إعادة استخدام الرابط\n{job.title or 'Media'}\nاختر عملية جديدة وسيتم إنشاء Job مستقل جديد."
    )
    await feature_module._edit_callback(callback, text, reuse_menu_keyboard(job_id))
    await callback.answer()


@router.callback_query(F.data.startswith("reuse:"))
async def reuse_action(callback: CallbackQuery) -> None:
    _, raw_id, action = callback.data.split(":", 2)
    source_job_id = int(raw_id)
    owned = await _owned_job(callback, source_job_id)
    if owned is None or callback.message is None:
        return
    _, user = owned
    try:
        clone, info = await _clone_ready_job(source_job_id, user.id, callback.message.chat.id)
    except (LookupError, ValueError) as exc:
        await callback.answer(str(exc)[:180], show_alert=True)
        return
    progress = await callback.message.answer(f"♻️ Job #{clone.id} • READY")
    if action == "video":
        await feature_module._show_analysis(callback.message, progress, clone, info, "video", user.language)
    elif action == "audio":
        await feature_module._show_analysis(callback.message, progress, clone, info, "audio", user.language)
    elif action == "cut":
        await feature_module._show_analysis(callback.message, progress, clone, info, "cut", user.language)
    elif action == "split":
        await progress.edit_text(
            f"🧩 Job #{clone.id}\nاختر طول المقطع المستمر:",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(text="30 ثانية", callback_data=f"advsplit:{clone.id}:30"),
                        InlineKeyboardButton(text="60 ثانية", callback_data=f"advsplit:{clone.id}:60"),
                    ]
                ]
            ),
        )
    else:
        await progress.edit_text("Unknown action")
    await callback.answer()


@router.callback_query(F.data.startswith("advsplit:"))
async def choose_split_size(callback: CallbackQuery) -> None:
    _, raw_id, raw_seconds = callback.data.split(":", 2)
    job_id = int(raw_id)
    segment_seconds = int(raw_seconds)
    owned = await _owned_job(callback, job_id)
    if owned is None:
        return
    job, user = owned
    if job.status != JobStatus.READY.value:
        await callback.answer("Job is not READY", show_alert=True)
        return
    text = (
        f"🧩 Continuous {segment_seconds}s split\nChoose video/audio and whether to split the whole media or a custom range."
        if user.language == "en"
        else f"🧩 تقسيم مستمر كل {segment_seconds} ثانية\nاختر فيديو أو صوت، ثم كامل الوسائط أو نطاقًا مخصصًا."
    )
    await feature_module._edit_callback(callback, text, split_choice_keyboard(job_id, segment_seconds))
    await callback.answer()


@router.callback_query(F.data.startswith("splitfull:"))
async def split_full(callback: CallbackQuery) -> None:
    _, raw_id, raw_seconds, quality = callback.data.split(":", 3)
    await _queue_split_feedback(
        callback,
        job_id=int(raw_id),
        segment_seconds=int(raw_seconds),
        quality=quality,
        start=0.0,
        end=None,
    )


@router.callback_query(F.data.startswith("splitrange:"))
async def split_range(callback: CallbackQuery, state: FSMContext) -> None:
    _, raw_id, raw_seconds, quality = callback.data.split(":", 3)
    job_id = int(raw_id)
    owned = await _owned_job(callback, job_id)
    if owned is None:
        return
    job, user = owned
    if job.status != JobStatus.READY.value:
        await callback.answer("Job is not READY", show_alert=True)
        return
    await state.set_state(AdvancedMediaState.waiting_split_range)
    await state.update_data(job_id=job_id, segment_seconds=int(raw_seconds), quality=quality)
    text = (
        "Send the range as 00:30 - 05:00. It will be split continuously inside that range."
        if user.language == "en"
        else "أرسل النطاق مثل 00:30 - 05:00 وسيتم تقسيمه بشكل متواصل داخل هذا النطاق."
    )
    await feature_module._edit_callback(callback, text)
    await callback.answer()


@router.message(AdvancedMediaState.waiting_split_range, F.text)
async def receive_split_range(message: Message, state: FSMContext) -> None:
    if parse_bulk_urls(message.text or "").urls:
        await state.clear()
        await feature_module._process_urls(message, intent="general")
        return
    if not message.from_user or not await is_allowed(message.from_user.id):
        await state.clear()
        return
    user = await ensure_user(message.from_user.id, message.from_user.username)
    data = await state.get_data()
    try:
        raw_start, raw_end = [part.strip() for part in (message.text or "").split("-", 1)]
        start = parse_time(raw_start)
        end = parse_time(raw_end)
        job_id = int(data["job_id"])
        segment_seconds = int(data["segment_seconds"])
        quality = str(data["quality"])
        start_value, end_value, count = await _configure_split(
            job_id,
            segment_seconds=segment_seconds,
            quality=quality,
            start=start,
            end=end,
        )
    except (KeyError, TypeError, ValueError, LookupError) as exc:
        text = (
            f"تعذر ضبط التقسيم: {str(exc)[:160]}\nاستخدم صيغة مثل 00:30 - 05:00."
            if user.language == "ar"
            else f"Could not configure split: {str(exc)[:160]}\nUse a range like 00:30 - 05:00."
        )
        await message.answer(text)
        return
    await state.clear()
    progress = await message.answer(
        f"🧩 Job #{job_id} • QUEUED\n"
        f"{count} parts × {segment_seconds}s\n"
        f"{seconds_to_hms(start_value)} → {seconds_to_hms(end_value)}",
        reply_markup=feature_module._cancel_keyboard(job_id),
    )
    await feature_module._update_progress_message(job_id, progress.message_id)


# Upgrade legacy entry points without duplicating their handlers.
ai_module.enhanced_menu_keyboard = public_menu_keyboard
handlers_module.main_menu_keyboard = public_menu_keyboard
feature_module.cut_menu_keyboard = advanced_cut_menu_keyboard

_ORIGINAL_PROCESS_DOWNLOAD = worker_module.process_download
_ORIGINAL_EDIT_STATUS = worker_module._edit_status


async def _edit_status_with_reuse(
    job_id: int,
    text: str,
    *,
    markup: InlineKeyboardMarkup | None = None,
) -> None:
    if markup is None and "Status: COMPLETED" in text:
        markup = completed_reuse_keyboard(job_id)
    await _ORIGINAL_EDIT_STATUS(job_id, text, markup=markup)


async def _split_job_mode(job_id: int) -> int | None:
    async with SessionLocal() as session:
        metadata = await session.get(MediaMetadata, job_id)
        mode = (metadata.cut_mode if metadata else "") or ""
    if mode == "SPLIT30":
        return 30
    if mode == "SPLIT60":
        return 60
    return None


async def _process_split_download(job_id: int, segment_seconds: int) -> None:
    import threading

    cancel_event = worker_module._cancel_events.setdefault(job_id, threading.Event())
    await worker_module._update_worker(1)
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
            job.error = None
            job.progress = 0.0
            await session.commit()
            source_url = job.source_url
            quality = job.selected_quality or "best"
            split_start = float(job.cut_start or 0.0)
            split_end = float(job.cut_end) if job.cut_end is not None else None
            chat_id = job.chat_id
            known_qualities = deserialize_qualities(metadata.formats_json if metadata else "[]")

        await record_job_event(job_id, JobStatus.DOWNLOADING.value, f"split={segment_seconds}; quality={quality}")
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
            if payload.get("status") != "downloading":
                return
            now = time.monotonic()
            if now - last_update < settings.progress_update_seconds:
                return
            last_update = now
            snapshot = ProgressSnapshot.from_ytdlp(payload)
            future = asyncio.run_coroutine_threadsafe(
                worker_module._persist_progress(job_id, snapshot, quality),
                loop,
            )
            future.add_done_callback(consume_future)

        source = await worker_module._download_with_retries(
            job_id,
            source_url,
            quality,
            job_dir,
            known_qualities,
            progress_hook,
            cancel_event,
        )
        if await worker_module._is_cancelled(job_id, cancel_event):
            raise CancelledError("Job cancelled")

        await set_job_status(
            job_id,
            JobStatus.CUTTING,
            event_message=f"continuous split {segment_seconds}s; {split_start}-{split_end or 'end'}",
        )
        await worker_module._edit_status(
            job_id,
            f"Job #{job_id}\nStatus: CUTTING\nContinuous: {segment_seconds}s segments",
            markup=worker_module._cancel_markup(job_id),
        )
        parts = await split_media(
            source,
            segment_seconds,
            start=split_start,
            end=split_end,
            cancel_event=cancel_event,
        )

        prepared = []
        for part in parts:
            if part.stat().st_size > settings.max_file_size_bytes:
                raise RuntimeError("File too large: split part exceeds MAX_FILE_SIZE_MB")
            output = part
            if chat_id != 0 and part.stat().st_size > settings.telegram_upload_limit_bytes:
                output = await worker_module.fit_media_for_upload(
                    part,
                    settings.telegram_upload_limit_bytes,
                    job_dir,
                    attempts=2,
                    cancel_event=cancel_event,
                )
            prepared.append(output)

        total_size = sum(item.stat().st_size for item in prepared)
        async with SessionLocal() as session:
            db_job = await session.get(DownloadJob, job_id)
            if db_job is None:
                raise LookupError("Job not found")
            db_job.output_path = str(prepared[0])
            db_job.file_size = total_size
            db_job.progress = 100.0
            db_job.status = JobStatus.UPLOADING.value if chat_id != 0 else JobStatus.COMPLETED.value
            await session.commit()

        if chat_id != 0:
            for index, output in enumerate(prepared, start=1):
                if await worker_module._is_cancelled(job_id, cancel_event):
                    raise CancelledError("Job cancelled")
                await worker_module._edit_status(
                    job_id,
                    f"Job #{job_id}\nStatus: UPLOADING\nPart: {index}/{len(prepared)}",
                    markup=worker_module._cancel_markup(job_id),
                )
                await worker_module._upload_with_retries(job_id, output, cancel_event)
            await set_job_status(job_id, JobStatus.COMPLETED, progress=100.0)
        else:
            await record_job_event(job_id, JobStatus.COMPLETED.value, f"split files={len(prepared)}; bytes={total_size}")

        await worker_module._edit_status(
            job_id,
            f"Job #{job_id}\nStatus: COMPLETED ✅\nParts: {len(prepared)} × {segment_seconds}s",
        )
    except Exception as exc:
        info = classify_error(exc)
        if info.code == ErrorCode.CANCELLED or cancel_event.is_set():
            await set_job_status(job_id, JobStatus.CANCELLED, error=ErrorCode.CANCELLED.value, event_message="cancelled")
            await worker_module._edit_status(job_id, f"Job #{job_id}\nStatus: CANCELLED")
        else:
            safe_message = redact_secrets(
                str(exc),
                bot_token=settings.bot_token,
                database_url=settings.database_url,
            )[:1500]
            await set_job_status(
                job_id,
                JobStatus.FAILED,
                error=f"{info.code.value}: {safe_message}",
                event_message=info.code.value,
            )
            await worker_module._edit_status(
                job_id,
                f"Job #{job_id}\nStatus: FAILED ❌\n{user_error_message(info)}",
                markup=worker_module._retry_markup(job_id),
            )
    finally:
        worker_module._cancel_events.pop(job_id, None)
        worker_module.downloader.forget(str(job_id))
        await worker_module._update_worker(-1)


async def _process_download_dispatch(job_id: int) -> None:
    segment_seconds = await _split_job_mode(job_id)
    if segment_seconds is None:
        await _ORIGINAL_PROCESS_DOWNLOAD(job_id)
        return
    await _process_split_download(job_id, segment_seconds)


worker_module._edit_status = _edit_status_with_reuse
worker_module.process_download = _process_download_dispatch
