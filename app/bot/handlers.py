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
from app.db import DownloadJob, JobStatus, MediaMetadata, SessionLocal, User
from app.errors import classify_error
from app.i18n import tr
from app.jobs import TERMINAL_STATUSES, record_job_event, set_job_status
from app.queue import cancel_download
from app.rate_limit import telegram_analyze_limiter
from app.services.downloader import MediaInfo, get_downloader_service
from app.services.job_service import (
    analyze_and_create_job,
    queue_existing_job,
)
from app.services.urls import parse_bulk_urls
from app.utils import parse_time, seconds_to_hms

settings = get_settings()
downloader = get_downloader_service()
router = Router()


class CutState(StatesGroup):
    waiting_range = State()


def main_menu_keyboard(language: str) -> InlineKeyboardMarkup:  # noqa: ARG001
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🎬 تحميل فيديو", callback_data="menu:video"),
                InlineKeyboardButton(text="🎵 MP3", callback_data="menu:audio"),
            ],
            [
                InlineKeyboardButton(text="✂️ قص", callback_data="menu:cut"),
                InlineKeyboardButton(text="📋 تحميلاتي", callback_data="history:0"),
            ],
            [InlineKeyboardButton(text="📥 تحميل عدة روابط", callback_data="menu:bulk")],
            [
                InlineKeyboardButton(text="🌐 اللغة", callback_data="menu:lang"),
                InlineKeyboardButton(text="ℹ️ مساعدة", callback_data="menu:help"),
            ],
        ]
    )


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
    buttons = [InlineKeyboardButton(text=f"{q}p", callback_data=f"q:{job_id}:{q}") for q in qualities]
    for index in range(0, len(buttons), 3):
        rows.append(buttons[index : index + 3])
    rows += [
        [
            InlineKeyboardButton(text="⭐ Best", callback_data=f"q:{job_id}:best"),
            InlineKeyboardButton(text="🎵 MP3", callback_data=f"q:{job_id}:audio"),
        ],
        [
            InlineKeyboardButton(text="✂️ FAST", callback_data=f"cut:{job_id}:FAST"),
            InlineKeyboardButton(text="🎯 PRECISE", callback_data=f"cut:{job_id}:PRECISE"),
        ],
        [InlineKeyboardButton(text="✖️ إلغاء", callback_data=f"cancel:{job_id}")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def playlist_keyboard(job_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="📥 Playlist Best", callback_data=f"playlist:{job_id}:best"),
                InlineKeyboardButton(text="🎵 Playlist MP3", callback_data=f"playlist:{job_id}:audio"),
            ],
            [InlineKeyboardButton(text="✖️ إلغاء", callback_data=f"cancel:{job_id}")],
        ]
    )


def cancel_keyboard(job_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="✖️ Cancel / إلغاء", callback_data=f"cancel:{job_id}")]]
    )


async def _message_user(message: Message) -> User | None:
    if not message.from_user:
        return None
    if not await is_allowed(message.from_user.id):
        await message.answer(tr(settings.default_language, "private"))
        return None
    return await ensure_user(message.from_user.id, message.from_user.username)


async def _callback_user(callback: CallbackQuery) -> User | None:
    if not callback.from_user or not await is_allowed(callback.from_user.id):
        await callback.answer("Private bot", show_alert=True)
        return None
    return await ensure_user(callback.from_user.id, callback.from_user.username)


def _info_text(job_id: int, info: MediaInfo) -> str:
    quality_text = ", ".join(f"{q}p" for q in info.qualities) or "Best / MP3"
    uploader = info.uploader or "—"
    if info.is_playlist:
        count = info.playlist_count
        suffix = "+" if count > settings.max_playlist_items else ""
        return (
            f"Job #{job_id}\n"
            f"Title: {info.title}\nPlatform: {info.platform}\nUploader: {uploader}\n"
            f"Playlist items detected: {count}{suffix}\nSafe limit: {settings.max_playlist_items}\n"
            "Confirm expansion; every item becomes an independent Job."
        )
    return (
        f"Job #{job_id}\n"
        f"Title: {info.title}\n"
        f"Platform: {info.platform}\n"
        f"Uploader: {uploader}\n"
        f"Duration: {seconds_to_hms(info.duration)}\n"
        f"Available: {quality_text}"
    )


async def _update_progress_message_id(job_id: int, message_id: int) -> None:
    async with SessionLocal() as session:
        job = await session.get(DownloadJob, job_id)
        if job is not None:
            job.progress_message_id = message_id
            await session.commit()


async def _present_info(message: Message, progress: Message, job: DownloadJob, info: MediaInfo) -> None:
    markup = playlist_keyboard(job.id) if info.is_playlist else quality_keyboard(job.id, info.qualities)
    text = _info_text(job.id, info)
    if info.thumbnail and info.thumbnail.startswith("https://") and not info.is_playlist:
        try:
            card = await message.answer_photo(photo=info.thumbnail, caption=text[:1024], reply_markup=markup)
            await _update_progress_message_id(job.id, card.message_id)
            try:
                await progress.delete()
            except Exception:
                pass
            return
        except Exception:
            pass
    await progress.edit_text(text, reply_markup=markup)


async def _analyze_one(message: Message, user: User, url: str) -> None:
    progress = await message.answer(tr(user.language, "analyzing"))
    try:
        job, info = await analyze_and_create_job(
            user_id=user.id,
            chat_id=message.chat.id,
            source_url=url,
            progress_message_id=progress.message_id,
            source="telegram",
        )
        if job.progress_message_id != progress.message_id:
            await progress.edit_text(f"Duplicate URL: existing Job #{job.id} is already active.")
            return
        await record_job_event(job.id, JobStatus.READY.value, f"qualities={info.qualities}; playlist={info.is_playlist}")
        if info.is_playlist and info.playlist_count > settings.max_playlist_items:
            await set_job_status(
                job.id,
                JobStatus.FAILED,
                error=f"PLAYLIST_LIMIT: max {settings.max_playlist_items}",
                event_message="PLAYLIST_LIMIT",
            )
            await progress.edit_text(
                f"Playlist is larger than the safe limit ({settings.max_playlist_items}). No items were queued."
            )
            return
        await _present_info(message, progress, job, info)
    except Exception as exc:
        error = classify_error(exc)
        await progress.edit_text(tr(user.language, "failed", code=error.code.value))


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
            await callback.answer("Job not found", show_alert=True)
            return None
        job, user = row
        session.expunge(job)
        session.expunge(user)
        return job, user


async def _history(user: User, page: int, size: int = 5) -> tuple[str, InlineKeyboardMarkup]:
    page = max(0, page)
    async with SessionLocal() as session:
        jobs = list(
            await session.scalars(
                select(DownloadJob)
                .where(DownloadJob.user_id == user.id)
                .order_by(DownloadJob.id.desc())
                .offset(page * size)
                .limit(size + 1)
            )
        )
    has_next = len(jobs) > size
    jobs = jobs[:size]
    if not jobs:
        text = tr(user.language, "jobs_empty")
    else:
        blocks = []
        for job in jobs:
            title = (job.title or "Untitled").replace("\n", " ")[:48]
            blocks.append(f"#{job.id} • {job.status}\n{title}\n{job.platform} • {job.selected_quality or '—'}")
        text = "📋 Downloads\n\n" + "\n\n".join(blocks)
    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"history:{page - 1}"))
    if has_next:
        nav.append(InlineKeyboardButton(text="➡️", callback_data=f"history:{page + 1}"))
    rows = [nav] if nav else []
    rows.append([InlineKeyboardButton(text="🏠", callback_data="menu:home")])
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


@router.message(CommandStart())
async def start(message: Message) -> None:
    user = await _message_user(message)
    if user:
        await message.answer(tr(user.language, "welcome", name=settings.app_name), reply_markup=main_menu_keyboard(user.language))


@router.message(Command("help"))
async def help_command(message: Message) -> None:
    user = await _message_user(message)
    if user:
        await message.answer(tr(user.language, "help"), reply_markup=main_menu_keyboard(user.language))


@router.message(Command("jobs"))
async def jobs_command(message: Message) -> None:
    user = await _message_user(message)
    if user:
        text, markup = await _history(user, 0)
        await message.answer(text, reply_markup=markup)


@router.callback_query(F.data == "menu:home")
async def home(callback: CallbackQuery) -> None:
    user = await _callback_user(callback)
    if user and callback.message:
        await callback.message.edit_text(tr(user.language, "welcome", name=settings.app_name), reply_markup=main_menu_keyboard(user.language))
    await callback.answer()


@router.callback_query(F.data.in_({"menu:video", "menu:audio", "menu:cut"}))
async def prompt_link(callback: CallbackQuery) -> None:
    user = await _callback_user(callback)
    if user and callback.message:
        await callback.message.edit_text(tr(user.language, "send_prompt"), reply_markup=main_menu_keyboard(user.language))
    await callback.answer()


@router.callback_query(F.data == "menu:bulk")
async def prompt_bulk(callback: CallbackQuery) -> None:
    user = await _callback_user(callback)
    if user and callback.message:
        await callback.message.edit_text(tr(user.language, "bulk_prompt"), reply_markup=main_menu_keyboard(user.language))
    await callback.answer()


@router.callback_query(F.data == "menu:help")
async def menu_help(callback: CallbackQuery) -> None:
    user = await _callback_user(callback)
    if user and callback.message:
        await callback.message.edit_text(tr(user.language, "help"), reply_markup=main_menu_keyboard(user.language))
    await callback.answer()


@router.callback_query(F.data == "menu:lang")
async def menu_lang(callback: CallbackQuery) -> None:
    user = await _callback_user(callback)
    if user and callback.message:
        await callback.message.edit_text("🌐 Language / اللغة", reply_markup=language_keyboard())
    await callback.answer()


@router.callback_query(F.data.startswith("lang:"))
async def set_language(callback: CallbackQuery) -> None:
    user = await _callback_user(callback)
    if user is None or not callback.from_user:
        return
    language = callback.data.split(":", 1)[1]
    if language not in {"ar", "en"}:
        await callback.answer()
        return
    async with SessionLocal() as session:
        db_user = await session.scalar(select(User).where(User.telegram_id == callback.from_user.id))
        if db_user:
            db_user.language = language
            await session.commit()
    if callback.message:
        await callback.message.edit_text(tr(language, "welcome", name=settings.app_name), reply_markup=main_menu_keyboard(language))
    await callback.answer()


@router.callback_query(F.data.startswith("history:"))
async def history_callback(callback: CallbackQuery) -> None:
    user = await _callback_user(callback)
    if user and callback.message:
        page = int(callback.data.split(":", 1)[1])
        text, markup = await _history(user, page)
        await callback.message.edit_text(text, reply_markup=markup)
    await callback.answer()


@router.callback_query(F.data.startswith("q:"))
async def choose_quality(callback: CallbackQuery) -> None:
    _, raw_id, quality = callback.data.split(":", 2)
    job_id = int(raw_id)
    owned = await _owned_job(callback, job_id)
    if owned is None:
        return
    _, user = owned
    try:
        await queue_existing_job(job_id, quality)
    except ValueError as exc:
        await callback.answer(str(exc), show_alert=True)
        return
    if callback.message:
        await callback.message.edit_text(tr(user.language, "queued", job_id=job_id), reply_markup=cancel_keyboard(job_id))
    await callback.answer()


@router.callback_query(F.data.startswith("cut:"))
async def choose_cut(callback: CallbackQuery, state: FSMContext) -> None:
    _, raw_id, mode = callback.data.split(":", 2)
    job_id = int(raw_id)
    owned = await _owned_job(callback, job_id)
    if owned is None:
        return
    job, user = owned
    if job.status != JobStatus.READY.value:
        await callback.answer("Job is not READY", show_alert=True)
        return
    await state.set_state(CutState.waiting_range)
    await state.update_data(job_id=job_id, mode=mode)
    if callback.message:
        await callback.message.edit_text(f"{tr(user.language, 'cut_prompt')}\nMode: {mode}")
    await callback.answer()


@router.message(CutState.waiting_range)
async def receive_cut_range(message: Message, state: FSMContext) -> None:
    user = await _message_user(message)
    if user is None or not message.text:
        return
    data = await state.get_data()
    job_id = int(data["job_id"])
    mode = str(data["mode"])
    try:
        raw_start, raw_end = [part.strip() for part in message.text.split("-", 1)]
        start = parse_time(raw_start)
        end = parse_time(raw_end)
        if end <= start:
            raise ValueError
        async with SessionLocal() as session:
            job = await session.get(DownloadJob, job_id)
            if job is None or job.user_id != user.id or job.status != JobStatus.READY.value:
                raise ValueError
            if job.duration is not None and end > job.duration:
                raise ValueError
            metadata = await session.get(MediaMetadata, job_id)
            job.cut_start = start
            job.cut_end = end
            if metadata is not None:
                metadata.cut_mode = mode
            await session.commit()
        await queue_existing_job(job_id, "best")
    except ValueError:
        await message.answer(tr(user.language, "cut_invalid"))
        return
    await state.clear()
    progress = await message.answer(tr(user.language, "queued", job_id=job_id), reply_markup=cancel_keyboard(job_id))
    await _update_progress_message_id(job_id, progress.message_id)


@router.callback_query(F.data.startswith("playlist:"))
async def expand_playlist(callback: CallbackQuery) -> None:
    _, raw_id, quality = callback.data.split(":", 2)
    parent_id = int(raw_id)
    owned = await _owned_job(callback, parent_id)
    if owned is None:
        return
    parent, user = owned
    if parent.status != JobStatus.READY.value:
        await callback.answer("Playlist is not READY", show_alert=True)
        return
    await callback.answer("Expanding playlist…")
    try:
        entries = await asyncio.to_thread(downloader.expand_playlist, parent.source_url, limit=settings.max_playlist_items)
    except Exception as exc:
        error = classify_error(exc)
        if callback.message:
            await callback.message.edit_text(f"Playlist expansion failed: {error.code.value}")
        return

    queued_ids: list[int] = []
    for entry in entries:
        child, child_info = await analyze_and_create_job(
            user_id=parent.user_id,
            chat_id=parent.chat_id,
            source_url=entry.url,
            source="telegram",
        )
        if child.status == JobStatus.READY.value:
            if callback.message:
                progress = await callback.message.answer(
                    f"Job #{child.id} • QUEUED\n{child_info.title[:120]}",
                    reply_markup=cancel_keyboard(child.id),
                )
                await _update_progress_message_id(child.id, progress.message_id)
            await queue_existing_job(child.id, quality)
        queued_ids.append(child.id)
    await set_job_status(parent_id, JobStatus.COMPLETED, progress=100.0, event_message=f"expanded children={queued_ids}")
    if callback.message:
        await callback.message.edit_text(f"Playlist expanded into {len(queued_ids)} independent jobs: {', '.join(map(str, queued_ids))}")


@router.callback_query(F.data.startswith("cancel:"))
async def cancel_job(callback: CallbackQuery) -> None:
    job_id = int(callback.data.split(":", 1)[1])
    owned = await _owned_job(callback, job_id)
    if owned is None:
        return
    job, _ = owned
    if job.status in TERMINAL_STATUSES:
        await callback.answer("Already finished", show_alert=True)
        return
    await set_job_status(job_id, JobStatus.CANCELLED, error="CANCELLED", event_message="cancel requested")
    await cancel_download(job_id)
    if callback.message:
        await callback.message.edit_text(f"Job #{job_id}\nStatus: CANCELLED")
    await callback.answer()


@router.callback_query(F.data.startswith("retry:"))
async def retry_job(callback: CallbackQuery) -> None:
    job_id = int(callback.data.split(":", 1)[1])
    owned = await _owned_job(callback, job_id)
    if owned is None:
        return
    job, user = owned
    if job.status != JobStatus.FAILED.value or not job.selected_quality:
        await callback.answer("Cannot retry this job", show_alert=True)
        return
    try:
        await queue_existing_job(job_id, job.selected_quality)
    except ValueError as exc:
        await callback.answer(str(exc), show_alert=True)
        return
    if callback.message:
        await callback.message.edit_text(tr(user.language, "queued", job_id=job_id), reply_markup=cancel_keyboard(job_id))
    await callback.answer()


@router.message(F.text)
async def analyze_text(message: Message) -> None:
    user = await _message_user(message)
    if user is None or not message.text:
        return
    if not await telegram_analyze_limiter.allow(f"tg:{user.telegram_id}"):
        await message.answer("Rate limit exceeded. Try again shortly.")
        return
    parsed = parse_bulk_urls(message.text)
    if not parsed.urls:
        return
    if parsed.duplicates:
        await message.answer(f"Deduplication: skipped {parsed.duplicates} duplicate URL(s).")
    for url in parsed.urls:
        await _analyze_one(message, user, url)
