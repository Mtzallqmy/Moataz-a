from __future__ import annotations

import asyncio

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import select

from app.bot.access import ensure_user, is_allowed
from app.config import get_settings
from app.db import DownloadJob, JobStatus, SessionLocal, User
from app.i18n import status_label, tr
from app.jobs import (
    TERMINAL_STATUSES,
    classify_job_error,
    count_user_running_jobs,
    record_job_event,
    set_job_status,
)
from app.queue import cancel_download, enqueue_download
from app.security import validate_media_url
from app.services.downloader import probe_media
from app.utils import parse_time, seconds_to_hms

settings = get_settings()
router = Router()


class CutState(StatesGroup):
    waiting_range = State()


def main_menu_keyboard(language: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=tr(language, "send_link_button"),
                    callback_data="menu:send",
                ),
                InlineKeyboardButton(
                    text=tr(language, "jobs_button"),
                    callback_data="menu:jobs",
                ),
            ],
            [
                InlineKeyboardButton(
                    text=tr(language, "language_button"),
                    callback_data="menu:lang",
                ),
                InlineKeyboardButton(
                    text=tr(language, "help_button"),
                    callback_data="menu:help",
                ),
            ],
        ]
    )


def language_keyboard(language: str = "ar") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🇸🇦 العربية", callback_data="lang:ar"),
                InlineKeyboardButton(text="🇬🇧 English", callback_data="lang:en"),
            ],
            [
                InlineKeyboardButton(
                    text=tr(language, "back_button"),
                    callback_data="menu:home",
                )
            ],
        ]
    )


def quality_keyboard(job_id: int, qualities: list[int], language: str) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    buttons = [
        InlineKeyboardButton(text=f"{quality}p", callback_data=f"q:{job_id}:{quality}")
        for quality in qualities
    ]
    for index in range(0, len(buttons), 2):
        rows.append(buttons[index : index + 2])
    rows.append(
        [
            InlineKeyboardButton(text="⭐ Best", callback_data=f"q:{job_id}:best"),
            InlineKeyboardButton(text="🎵 MP3", callback_data=f"q:{job_id}:audio"),
        ]
    )
    rows.append([InlineKeyboardButton(text="✂️ Cut / قص", callback_data=f"cut:{job_id}")])
    rows.append(
        [
            InlineKeyboardButton(
                text=tr(language, "cancel_button"),
                callback_data=f"cancel:{job_id}",
            )
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def cancel_keyboard(job_id: int, language: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=tr(language, "cancel_button"),
                    callback_data=f"cancel:{job_id}",
                )
            ]
        ]
    )


async def _user_for_message(message: Message) -> User | None:
    if not message.from_user:
        return None
    if not await is_allowed(message.from_user.id):
        await message.answer(tr(settings.default_language, "private"))
        return None
    return await ensure_user(message.from_user.id, message.from_user.username)


async def _user_for_callback(callback: CallbackQuery) -> User | None:
    if not callback.from_user or not await is_allowed(callback.from_user.id):
        await callback.answer("Private bot", show_alert=True)
        return None
    return await ensure_user(callback.from_user.id, callback.from_user.username)


async def _recent_jobs_text(user: User, limit: int = 8) -> str:
    async with SessionLocal() as session:
        jobs = list(
            await session.scalars(
                select(DownloadJob)
                .where(DownloadJob.user_id == user.id)
                .order_by(DownloadJob.id.desc())
                .limit(limit)
            )
        )
    if not jobs:
        return tr(user.language, "jobs_empty")
    lines = []
    for job in jobs:
        title = (job.title or "Untitled").replace("\n", " ").strip()[:42]
        quality = job.selected_quality or "—"
        lines.append(
            f"#{job.id} • {status_label(user.language, job.status)}\n"
            f"{title}\n"
            f"{job.platform} • {quality}"
        )
    return tr(user.language, "jobs_header", jobs="\n\n".join(lines))


@router.message(CommandStart())
async def start(message: Message) -> None:
    user = await _user_for_message(message)
    if not user:
        return
    await message.answer(
        tr(user.language, "welcome", name=settings.app_name),
        reply_markup=main_menu_keyboard(user.language),
    )


@router.message(Command("help"))
async def help_command(message: Message) -> None:
    user = await _user_for_message(message)
    if not user:
        return
    await message.answer(tr(user.language, "help"), reply_markup=main_menu_keyboard(user.language))


@router.message(Command("jobs"))
async def jobs_command(message: Message) -> None:
    user = await _user_for_message(message)
    if not user:
        return
    await message.answer(
        await _recent_jobs_text(user),
        reply_markup=main_menu_keyboard(user.language),
    )


@router.callback_query(F.data == "menu:home")
async def menu_home(callback: CallbackQuery) -> None:
    user = await _user_for_callback(callback)
    if not user:
        return
    if callback.message:
        await callback.message.edit_text(
            tr(user.language, "welcome", name=settings.app_name),
            reply_markup=main_menu_keyboard(user.language),
        )
    await callback.answer()


@router.callback_query(F.data == "menu:send")
async def menu_send(callback: CallbackQuery) -> None:
    user = await _user_for_callback(callback)
    if not user:
        return
    if callback.message:
        await callback.message.edit_text(
            tr(user.language, "send_link_prompt"),
            reply_markup=main_menu_keyboard(user.language),
        )
    await callback.answer()


@router.callback_query(F.data == "menu:jobs")
async def menu_jobs(callback: CallbackQuery) -> None:
    user = await _user_for_callback(callback)
    if not user:
        return
    if callback.message:
        await callback.message.edit_text(
            await _recent_jobs_text(user),
            reply_markup=main_menu_keyboard(user.language),
        )
    await callback.answer()


@router.callback_query(F.data == "menu:help")
async def menu_help(callback: CallbackQuery) -> None:
    user = await _user_for_callback(callback)
    if not user:
        return
    if callback.message:
        await callback.message.edit_text(
            tr(user.language, "help"),
            reply_markup=main_menu_keyboard(user.language),
        )
    await callback.answer()


@router.callback_query(F.data == "menu:lang")
async def menu_language(callback: CallbackQuery) -> None:
    user = await _user_for_callback(callback)
    if not user:
        return
    if callback.message:
        await callback.message.edit_text(
            tr(user.language, "language_button"),
            reply_markup=language_keyboard(user.language),
        )
    await callback.answer()


@router.callback_query(F.data.startswith("lang:"))
async def set_language(callback: CallbackQuery) -> None:
    if not callback.from_user or not await is_allowed(callback.from_user.id):
        await callback.answer("Private bot", show_alert=True)
        return
    language = callback.data.split(":", 1)[1]
    if language not in {"ar", "en"}:
        await callback.answer()
        return
    async with SessionLocal() as session:
        user = await session.scalar(select(User).where(User.telegram_id == callback.from_user.id))
        if user is None:
            user = await ensure_user(callback.from_user.id, callback.from_user.username)
        else:
            user.language = language
            await session.commit()
    if callback.message:
        await callback.message.edit_text(
            tr(language, "welcome", name=settings.app_name),
            reply_markup=main_menu_keyboard(language),
        )
    await callback.answer(tr(language, "language_changed"))


@router.message(F.text.regexp(r"^https?://"))
async def analyze_url(message: Message) -> None:
    user = await _user_for_message(message)
    if not user or not message.text:
        return
    url = message.text.strip()
    if not validate_media_url(url):
        await message.answer(tr(user.language, "invalid_url"))
        return

    progress = await message.answer(tr(user.language, "analyzing"))
    async with SessionLocal() as session:
        job = DownloadJob(
            user_id=user.id,
            chat_id=message.chat.id,
            progress_message_id=progress.message_id,
            source_url=url,
            status=JobStatus.ANALYZING.value,
        )
        session.add(job)
        await session.commit()
        await session.refresh(job)
        job_id = job.id
    await record_job_event(job_id, JobStatus.ANALYZING.value, "URL received")

    try:
        info = await asyncio.to_thread(probe_media, url)
        if info.duration and info.duration > settings.max_video_duration_seconds:
            await set_job_status(
                job_id,
                JobStatus.FAILED,
                error="MEDIA_TOO_LONG: Duration limit exceeded",
                event_message="MEDIA_TOO_LONG",
            )
            await progress.edit_text(tr(user.language, "too_long"))
            return

        async with SessionLocal() as session:
            db_job = await session.get(DownloadJob, job_id)
            if not db_job or db_job.status == JobStatus.CANCELLED.value:
                return
            db_job.title = info.title
            db_job.duration = info.duration
            db_job.thumbnail = info.thumbnail
            db_job.platform = info.platform
            db_job.status = JobStatus.READY.value
            await session.commit()
        await record_job_event(job_id, JobStatus.READY.value, f"qualities={info.qualities}")

        await progress.edit_text(
            tr(
                user.language,
                "choose_quality",
                title=info.title,
                platform=info.platform,
                duration=seconds_to_hms(info.duration),
                job_id=job_id,
            ),
            reply_markup=quality_keyboard(job_id, info.qualities, user.language),
        )
    except Exception as exc:
        info = classify_job_error(exc)
        await set_job_status(
            job_id,
            JobStatus.FAILED,
            error=f"{info.code}: {str(exc)[:1800]}",
            event_message=info.code,
        )
        await progress.edit_text(tr(user.language, "unsupported"))


async def _owned_job(callback: CallbackQuery, job_id: int) -> tuple[DownloadJob, User] | None:
    if not callback.from_user or not await is_allowed(callback.from_user.id):
        await callback.answer("Private bot", show_alert=True)
        return None
    async with SessionLocal() as session:
        result = await session.execute(
            select(DownloadJob, User)
            .join(User, DownloadJob.user_id == User.id)
            .where(DownloadJob.id == job_id, User.telegram_id == callback.from_user.id)
        )
        row = result.first()
        if not row:
            user = await ensure_user(callback.from_user.id, callback.from_user.username)
            await callback.answer(tr(user.language, "job_not_found"), show_alert=True)
            return None
        job, user = row
        session.expunge(job)
        session.expunge(user)
        return job, user


async def _check_active_limit(user_id: int, job_id: int, language: str) -> str | None:
    active = await count_user_running_jobs(user_id, exclude_job_id=job_id)
    if active >= settings.max_jobs_per_user:
        return tr(language, "active_limit", limit=settings.max_jobs_per_user)
    return None


@router.callback_query(F.data.startswith("q:"))
async def choose_quality(callback: CallbackQuery) -> None:
    _, raw_job_id, quality = callback.data.split(":", 2)
    job_id = int(raw_job_id)
    owned = await _owned_job(callback, job_id)
    if not owned:
        return
    job_snapshot, user = owned
    limit_error = await _check_active_limit(job_snapshot.user_id, job_id, user.language)
    if limit_error:
        await callback.answer(limit_error, show_alert=True)
        return

    async with SessionLocal() as session:
        job = await session.get(DownloadJob, job_id)
        if not job or job.status not in {JobStatus.READY.value, JobStatus.FAILED.value}:
            await callback.answer(tr(user.language, "already_queued"), show_alert=True)
            return
        job.selected_quality = quality
        job.status = JobStatus.QUEUED.value
        job.error = None
        job.progress = 0.0
        if callback.message:
            job.progress_message_id = callback.message.message_id
        await session.commit()
    await record_job_event(job_id, JobStatus.QUEUED.value, f"quality={quality}")
    await enqueue_download(job_id)
    if callback.message:
        await callback.message.edit_text(
            tr(user.language, "queued", job_id=job_id),
            reply_markup=cancel_keyboard(job_id, user.language),
        )
    await callback.answer()


@router.callback_query(F.data.startswith("cancel:"))
async def cancel_job(callback: CallbackQuery) -> None:
    job_id = int(callback.data.split(":", 1)[1])
    owned = await _owned_job(callback, job_id)
    if not owned:
        return
    job_snapshot, user = owned
    if job_snapshot.status in TERMINAL_STATUSES:
        await callback.answer(tr(user.language, "cannot_cancel"), show_alert=True)
        return

    await set_job_status(
        job_id,
        JobStatus.CANCELLED,
        event_message="cancel requested from Telegram",
    )
    await cancel_download(job_id)
    if callback.message:
        await callback.message.edit_text(tr(user.language, "cancelled"))
    await callback.answer(tr(user.language, "cancel_requested", job_id=job_id))


@router.callback_query(F.data.startswith("retry:"))
async def retry_job(callback: CallbackQuery) -> None:
    job_id = int(callback.data.split(":", 1)[1])
    owned = await _owned_job(callback, job_id)
    if not owned:
        return
    job_snapshot, user = owned
    if job_snapshot.status != JobStatus.FAILED.value or not job_snapshot.selected_quality:
        await callback.answer(tr(user.language, "cannot_retry"), show_alert=True)
        return
    limit_error = await _check_active_limit(job_snapshot.user_id, job_id, user.language)
    if limit_error:
        await callback.answer(limit_error, show_alert=True)
        return

    async with SessionLocal() as session:
        job = await session.get(DownloadJob, job_id)
        if not job or job.status != JobStatus.FAILED.value:
            await callback.answer(tr(user.language, "cannot_retry"), show_alert=True)
            return
        job.status = JobStatus.QUEUED.value
        job.error = None
        job.progress = 0.0
        job.speed = None
        job.eta = None
        if callback.message:
            job.progress_message_id = callback.message.message_id
        await session.commit()
    await record_job_event(job_id, "MANUAL_RETRY", "retry requested from Telegram")
    await enqueue_download(job_id)
    if callback.message:
        await callback.message.edit_text(
            tr(user.language, "retry_queued", job_id=job_id),
            reply_markup=cancel_keyboard(job_id, user.language),
        )
    await callback.answer()


@router.callback_query(F.data.startswith("cut:"))
async def choose_cut(callback: CallbackQuery, state: FSMContext) -> None:
    job_id = int(callback.data.split(":", 1)[1])
    owned = await _owned_job(callback, job_id)
    if not owned:
        return
    job, user = owned
    if job.status != JobStatus.READY.value:
        await callback.answer(tr(user.language, "already_queued"), show_alert=True)
        return
    await state.set_state(CutState.waiting_range)
    await state.update_data(job_id=job_id)
    if callback.message:
        await callback.message.edit_text(tr(user.language, "cut_prompt"))
    await callback.answer()


@router.message(CutState.waiting_range)
async def receive_cut_range(message: Message, state: FSMContext) -> None:
    user = await _user_for_message(message)
    if not user or not message.text:
        return
    try:
        raw_start, raw_end = [part.strip() for part in message.text.split("-", 1)]
        start = parse_time(raw_start)
        end = parse_time(raw_end)
        if end <= start:
            raise ValueError
    except (ValueError, IndexError):
        await message.answer(tr(user.language, "cut_invalid"))
        return

    data = await state.get_data()
    job_id = int(data["job_id"])
    async with SessionLocal() as session:
        job = await session.get(DownloadJob, job_id)
        if not job or job.user_id != user.id:
            await state.clear()
            return
        if job.status != JobStatus.READY.value:
            await state.clear()
            await message.answer(tr(user.language, "already_queued"))
            return
        if job.duration and end > job.duration:
            await message.answer(tr(user.language, "cut_invalid"))
            return
        limit_error = await _check_active_limit(job.user_id, job.id, user.language)
        if limit_error:
            await message.answer(limit_error)
            return
        job.cut_start = start
        job.cut_end = end
        job.selected_quality = "720"
        job.status = JobStatus.QUEUED.value
        progress = await message.answer(
            tr(user.language, "cut_queued", job_id=job_id),
            reply_markup=cancel_keyboard(job_id, user.language),
        )
        job.progress_message_id = progress.message_id
        await session.commit()
    await record_job_event(job_id, JobStatus.QUEUED.value, f"clip={start}-{end}")
    await state.clear()
    await enqueue_download(job_id)
