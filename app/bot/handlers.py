from __future__ import annotations

import asyncio

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import func, select

from app.bot.access import ensure_user, is_allowed
from app.config import get_settings
from app.db import DownloadJob, JobStatus, SessionLocal, User
from app.i18n import tr
from app.queue import enqueue_download
from app.security import validate_media_url
from app.services.downloader import probe_media
from app.utils import parse_time, seconds_to_hms

settings = get_settings()
router = Router()


class CutState(StatesGroup):
    waiting_range = State()


def language_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🇸🇦 العربية", callback_data="lang:ar"),
                InlineKeyboardButton(text="🇬🇧 English", callback_data="lang:en"),
            ]
        ]
    )


def quality_keyboard(job_id: int, qualities: list[int]) -> InlineKeyboardMarkup:
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
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _user_for_message(message: Message) -> User | None:
    if not message.from_user:
        return None
    if not await is_allowed(message.from_user.id):
        await message.answer(tr(settings.default_language, "private"))
        return None
    return await ensure_user(message.from_user.id, message.from_user.username)


@router.message(CommandStart())
async def start(message: Message) -> None:
    user = await _user_for_message(message)
    if not user:
        return
    await message.answer(
        tr(user.language, "welcome", name=settings.app_name), reply_markup=language_keyboard()
    )


@router.message(Command("help"))
async def help_command(message: Message) -> None:
    user = await _user_for_message(message)
    if not user:
        return
    text = (
        "أرسل رابط YouTube/Facebook ثم اختر الجودة. يمكنك أيضًا اختيار زر القص.\n"
        "Send a YouTube/Facebook URL, choose a quality, or use the clip button."
    )
    await message.answer(text)


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
    await callback.answer(tr(language, "language_changed"), show_alert=True)


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
            status=JobStatus.PROBING.value,
        )
        session.add(job)
        await session.commit()
        await session.refresh(job)
        job_id = job.id

    try:
        info = await asyncio.to_thread(probe_media, url)
        if info.duration and info.duration > settings.max_video_duration_seconds:
            async with SessionLocal() as session:
                db_job = await session.get(DownloadJob, job_id)
                db_job.status = JobStatus.FAILED.value
                db_job.error = "Duration limit exceeded"
                await session.commit()
            await progress.edit_text(tr(user.language, "too_long"))
            return

        async with SessionLocal() as session:
            db_job = await session.get(DownloadJob, job_id)
            db_job.title = info.title
            db_job.duration = info.duration
            db_job.thumbnail = info.thumbnail
            db_job.platform = info.platform
            db_job.status = JobStatus.READY.value
            await session.commit()

        await progress.edit_text(
            tr(
                user.language,
                "choose_quality",
                title=info.title,
                duration=seconds_to_hms(info.duration),
            ),
            reply_markup=quality_keyboard(job_id, info.qualities),
        )
    except Exception as exc:
        async with SessionLocal() as session:
            db_job = await session.get(DownloadJob, job_id)
            if db_job:
                db_job.status = JobStatus.FAILED.value
                db_job.error = str(exc)[:1000]
                await session.commit()
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
            await callback.answer("Job not found", show_alert=True)
            return None
        job, user = row
        session.expunge(job)
        session.expunge(user)
        return job, user


@router.callback_query(F.data.startswith("q:"))
async def choose_quality(callback: CallbackQuery) -> None:
    _, raw_job_id, quality = callback.data.split(":", 2)
    job_id = int(raw_job_id)
    owned = await _owned_job(callback, job_id)
    if not owned:
        return
    _, user = owned
    async with SessionLocal() as session:
        job = await session.get(DownloadJob, job_id)
        if job.status not in {JobStatus.READY.value, JobStatus.FAILED.value}:
            await callback.answer("Already queued", show_alert=True)
            return
        active = await session.scalar(
            select(func.count()).select_from(DownloadJob).where(
                DownloadJob.user_id == job.user_id,
                DownloadJob.id != job.id,
                DownloadJob.status.in_([
                    JobStatus.QUEUED.value, JobStatus.DOWNLOADING.value,
                    JobStatus.PROCESSING.value, JobStatus.UPLOADING.value,
                ]),
            )
        ) or 0
        if active >= settings.max_jobs_per_user:
            await callback.answer(
                f"Active job limit: {settings.max_jobs_per_user}", show_alert=True
            )
            return
        job.selected_quality = quality
        job.status = JobStatus.QUEUED.value
        if callback.message:
            job.progress_message_id = callback.message.message_id
        await session.commit()
    await enqueue_download(job_id)
    if callback.message:
        await callback.message.edit_text(tr(user.language, "queued"))
    await callback.answer()


@router.callback_query(F.data.startswith("cut:"))
async def choose_cut(callback: CallbackQuery, state: FSMContext) -> None:
    job_id = int(callback.data.split(":", 1)[1])
    owned = await _owned_job(callback, job_id)
    if not owned:
        return
    _, user = owned
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
        if job.duration and end > job.duration:
            await message.answer(tr(user.language, "cut_invalid"))
            return
        active = await session.scalar(
            select(func.count()).select_from(DownloadJob).where(
                DownloadJob.user_id == job.user_id,
                DownloadJob.id != job.id,
                DownloadJob.status.in_([
                    JobStatus.QUEUED.value, JobStatus.DOWNLOADING.value,
                    JobStatus.PROCESSING.value, JobStatus.UPLOADING.value,
                ]),
            )
        ) or 0
        if active >= settings.max_jobs_per_user:
            await message.answer(f"Active job limit: {settings.max_jobs_per_user}")
            return
        job.cut_start = start
        job.cut_end = end
        job.selected_quality = "720"
        job.status = JobStatus.QUEUED.value
        progress = await message.answer(tr(user.language, "cut_queued"))
        job.progress_message_id = progress.message_id
        await session.commit()
    await state.clear()
    await enqueue_download(job_id)
