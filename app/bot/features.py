from __future__ import annotations

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import BaseFilter, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import select

from app.bot.access import ensure_user, is_allowed
from app.config import get_settings
from app.db import DownloadJob, JobStatus, MediaMetadata, SessionLocal, User
from app.errors import classify_error
from app.i18n import tr
from app.jobs import set_job_status
from app.rate_limit import telegram_analyze_limiter
from app.services.cutting import configure_and_queue_cut
from app.services.downloader import MediaInfo
from app.services.job_service import analyze_and_create_job, deserialize_qualities, queue_existing_job
from app.services.urls import parse_bulk_urls
from app.utils import parse_time, seconds_to_hms

settings = get_settings()
router = Router(name="feature-flows")


class FeatureState(StatesGroup):
    waiting_url = State()
    waiting_cut_input = State()


class ContainsMediaURL(BaseFilter):
    async def __call__(self, message: Message) -> bool:
        return bool(message.text and parse_bulk_urls(message.text).urls)


def _cancel_keyboard(job_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="✖️ إلغاء", callback_data=f"cancel:{job_id}")]]
    )


def _quality_rows(job_id: int, qualities: list[int], *, include_audio: bool) -> list[list[InlineKeyboardButton]]:
    rows: list[list[InlineKeyboardButton]] = []
    buttons = [InlineKeyboardButton(text=f"{quality}p", callback_data=f"q:{job_id}:{quality}") for quality in qualities]
    for index in range(0, len(buttons), 3):
        rows.append(buttons[index : index + 3])
    first = [InlineKeyboardButton(text="⭐ أفضل جودة", callback_data=f"q:{job_id}:best")]
    if include_audio:
        first.append(InlineKeyboardButton(text="🎵 MP3", callback_data=f"q:{job_id}:audio"))
    rows.append(first)
    rows.append([InlineKeyboardButton(text="✂️ خيارات القص", callback_data=f"cutmenu:{job_id}:PRECISE")])
    rows.append([InlineKeyboardButton(text="✖️ إلغاء", callback_data=f"cancel:{job_id}")])
    return rows


def media_keyboard(job_id: int, qualities: list[int], *, include_audio: bool = True) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=_quality_rows(job_id, qualities, include_audio=include_audio))


def cut_menu_keyboard(job_id: int, mode: str = "PRECISE") -> InlineKeyboardMarkup:
    normalized_mode = "FAST" if mode.upper() == "FAST" else "PRECISE"
    other_mode = "PRECISE" if normalized_mode == "FAST" else "FAST"
    mode_label = "⚡ سريع FAST" if normalized_mode == "FAST" else "🎯 دقيق PRECISE"
    other_label = "🎯 استخدم PRECISE" if other_mode == "PRECISE" else "⚡ استخدم FAST"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✂️ قص حر", callback_data=f"cutpick:{job_id}:free:{normalized_mode}")],
            [InlineKeyboardButton(text="📱 30 ثانية للقصص", callback_data=f"cutpick:{job_id}:30:{normalized_mode}")],
            [InlineKeyboardButton(text="🎞️ 60 ثانية", callback_data=f"cutpick:{job_id}:60:{normalized_mode}")],
            [
                InlineKeyboardButton(text=f"الوضع: {mode_label}", callback_data=f"cutmenu:{job_id}:{normalized_mode}"),
                InlineKeyboardButton(text=other_label, callback_data=f"cutmenu:{job_id}:{other_mode}"),
            ],
            [InlineKeyboardButton(text="⬅️ خيارات التنزيل", callback_data=f"cutback:{job_id}")],
        ]
    )


def _info_text(job_id: int, info: MediaInfo, language: str = "ar") -> str:
    qualities = ", ".join(f"{quality}p" for quality in info.qualities) or "Best / MP3"
    uploader = info.uploader or "—"
    if language == "en":
        return (
            f"Job #{job_id}\n"
            f"Title: {info.title}\n"
            f"Platform: {info.platform}\n"
            f"Uploader: {uploader}\n"
            f"Duration: {seconds_to_hms(info.duration)}\n"
            f"Available: {qualities}"
        )
    return (
        f"Job #{job_id}\n"
        f"العنوان: {info.title}\n"
        f"المنصة: {info.platform}\n"
        f"الناشر: {uploader}\n"
        f"المدة: {seconds_to_hms(info.duration)}\n"
        f"الجودات المتاحة: {qualities}"
    )


def _playlist_keyboard(job_id: int, preferred: str = "best") -> InlineKeyboardMarkup:
    audio_text = "🎵 تنزيل القائمة MP3 ✓" if preferred == "audio" else "🎵 تنزيل القائمة MP3"
    best_text = "📥 تنزيل القائمة Best ✓" if preferred == "best" else "📥 تنزيل القائمة Best"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=best_text, callback_data=f"playlist:{job_id}:best")],
            [InlineKeyboardButton(text=audio_text, callback_data=f"playlist:{job_id}:audio")],
            [InlineKeyboardButton(text="✖️ إلغاء", callback_data=f"cancel:{job_id}")],
        ]
    )


async def _get_message_user(message: Message) -> User | None:
    if not message.from_user:
        return None
    if not await is_allowed(message.from_user.id):
        await message.answer(tr(settings.default_language, "private"))
        return None
    return await ensure_user(message.from_user.id, message.from_user.username)


async def _get_callback_user(callback: CallbackQuery) -> User | None:
    if not callback.from_user or not await is_allowed(callback.from_user.id):
        await callback.answer("Private bot", show_alert=True)
        return None
    return await ensure_user(callback.from_user.id, callback.from_user.username)


async def _get_callback_job(callback: CallbackQuery, job_id: int) -> tuple[DownloadJob, User] | None:
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


async def _edit_callback(callback: CallbackQuery, text: str, markup: InlineKeyboardMarkup | None = None) -> None:
    if callback.message is None:
        return
    try:
        await callback.message.edit_text(text, reply_markup=markup)
        return
    except TelegramBadRequest:
        pass
    try:
        await callback.message.edit_caption(caption=text[:1024], reply_markup=markup)
    except TelegramBadRequest:
        pass


async def _update_progress_message(job_id: int, message_id: int) -> None:
    async with SessionLocal() as session:
        job = await session.get(DownloadJob, job_id)
        if job is not None:
            job.progress_message_id = message_id
            await session.commit()


async def _deliver_card(
    message: Message,
    progress: Message,
    job: DownloadJob,
    info: MediaInfo,
    text: str,
    markup: InlineKeyboardMarkup,
) -> None:
    if info.thumbnail and info.thumbnail.startswith("https://") and not info.is_playlist:
        try:
            card = await message.answer_photo(
                photo=info.thumbnail,
                caption=text[:1024],
                reply_markup=markup,
            )
            await _update_progress_message(job.id, card.message_id)
            try:
                await progress.delete()
            except Exception:
                pass
            return
        except Exception:
            pass
    await progress.edit_text(text, reply_markup=markup)


async def _show_analysis(
    message: Message,
    progress: Message,
    job: DownloadJob,
    info: MediaInfo,
    intent: str,
    language: str,
) -> None:
    if info.is_playlist:
        if language == "en":
            text = (
                f"Job #{job.id}\n{info.title}\n"
                f"Playlist: {info.playlist_count} items\n"
                f"Safe limit: {settings.max_playlist_items}\n"
                "Confirm expansion; every item becomes an independent Job."
            )
        else:
            text = (
                f"Job #{job.id}\n{info.title}\n"
                f"Playlist: {info.playlist_count} عنصر\n"
                f"الحد الآمن: {settings.max_playlist_items}\n"
                "يجب تأكيد التوسعة؛ كل عنصر سيصبح Job مستقلاً."
            )
        await progress.edit_text(
            text,
            reply_markup=_playlist_keyboard(job.id, preferred="audio" if intent == "audio" else "best"),
        )
        return

    text = _info_text(job.id, info, language)
    if intent == "audio":
        await queue_existing_job(job.id, "audio")
        queued = tr(language, "queued", job_id=job.id)
        await _deliver_card(
            message,
            progress,
            job,
            info,
            f"{text}\n\n{queued}",
            _cancel_keyboard(job.id),
        )
        return

    if intent == "cut":
        hint = (
            "Choose a cut type. PRECISE is frame-accurate; FAST is faster without re-encoding."
            if language == "en"
            else "اختر نوع القص. PRECISE أدق، وFAST أسرع بدون إعادة ترميز."
        )
        await _deliver_card(
            message,
            progress,
            job,
            info,
            f"{text}\n\n{hint}",
            cut_menu_keyboard(job.id, "PRECISE"),
        )
        return

    await _deliver_card(
        message,
        progress,
        job,
        info,
        text,
        media_keyboard(job.id, info.qualities, include_audio=intent != "video"),
    )


async def _analyze_one(message: Message, user: User, url: str, intent: str = "general") -> None:
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
            duplicate = (
                f"Duplicate URL: existing Job #{job.id} is already active."
                if user.language == "en"
                else f"الرابط موجود بالفعل في Job #{job.id} النشط."
            )
            await progress.edit_text(duplicate)
            return
        if info.is_playlist and info.playlist_count > settings.max_playlist_items:
            await set_job_status(
                job.id,
                JobStatus.FAILED,
                error=f"PLAYLIST_LIMIT: max {settings.max_playlist_items}",
                event_message="PLAYLIST_LIMIT",
            )
            limit_text = (
                f"Playlist is larger than the safe limit ({settings.max_playlist_items})."
                if user.language == "en"
                else f"القائمة أكبر من الحد الآمن ({settings.max_playlist_items})."
            )
            await progress.edit_text(limit_text)
            return
        await _show_analysis(message, progress, job, info, intent, user.language)
    except Exception as exc:
        error = classify_error(exc)
        await progress.edit_text(tr(user.language, "failed", code=error.code.value))


async def _process_urls(message: Message, *, intent: str = "general") -> None:
    user = await _get_message_user(message)
    if user is None or not message.text:
        return
    if not await telegram_analyze_limiter.allow(f"tg:{user.telegram_id}"):
        text = "Rate limit exceeded. Try again shortly." if user.language == "en" else "تم تجاوز معدل الطلبات. حاول بعد قليل."
        await message.answer(text)
        return
    parsed = parse_bulk_urls(message.text, limit=settings.max_bulk_urls)
    if not parsed.urls:
        await message.answer(tr(user.language, "invalid_url"))
        return
    if parsed.duplicates:
        text = (
            f"Deduplication: skipped {parsed.duplicates} duplicate URL(s)."
            if user.language == "en"
            else f"تم تجاهل {parsed.duplicates} رابط مكرر."
        )
        await message.answer(text)
    effective_intent = intent if len(parsed.urls) == 1 else ("audio" if intent == "audio" else "general")
    for url in parsed.urls:
        await _analyze_one(message, user, url, effective_intent)


@router.message(CommandStart())
async def reset_and_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    from app.bot.handlers import start

    await start(message)


@router.callback_query(F.data == "menu:home")
async def reset_home(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    from app.bot.handlers import home

    await home(callback)


@router.callback_query(F.data.in_({"menu:video", "menu:audio", "menu:cut", "menu:bulk"}))
async def choose_next_action(callback: CallbackQuery, state: FSMContext) -> None:
    user = await _get_callback_user(callback)
    if user is None:
        return
    intent = callback.data.split(":", 1)[1]
    await state.set_state(FeatureState.waiting_url)
    await state.update_data(intent=intent)
    if callback.message:
        if user.language == "en":
            prompts = {
                "video": "🎬 Send the media URL. I will analyze it and show only resolutions that actually exist.",
                "audio": "🎵 Send the URL. I will analyze it and queue MP3 automatically.",
                "cut": "✂️ Send the video URL, then choose Free Cut, 30-second Story, or 60-second Cut.",
                "bulk": "📥 Send multiple URLs separated by spaces or new lines.",
            }
        else:
            prompts = {
                "video": "🎬 أرسل رابط الفيديو. بعد التحليل سأعرض الجودات المتاحة فعليًا.",
                "audio": "🎵 أرسل الرابط وسأحلله ثم أضيف تنزيل MP3 مباشرة.",
                "cut": "✂️ أرسل رابط الفيديو ثم اختر: قص حر، 30 ثانية للقصص، أو 60 ثانية.",
                "bulk": "📥 أرسل عدة روابط، كل رابط في سطر أو مفصول بمسافة.",
            }
        await _edit_callback(callback, prompts[intent])
    await callback.answer()


@router.message(FeatureState.waiting_url, F.text)
async def receive_action_url(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    intent = str(data.get("intent") or "general")
    parsed = parse_bulk_urls(message.text or "", limit=settings.max_bulk_urls)
    if not parsed.urls:
        user = await _get_message_user(message)
        if user is not None:
            await message.answer(tr(user.language, "invalid_url"))
        return
    await state.clear()
    await _process_urls(message, intent=intent)


@router.message(FeatureState.waiting_cut_input, F.text)
async def receive_cut_input(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    if parse_bulk_urls(message.text or "").urls:
        await state.clear()
        await _process_urls(message, intent="general")
        return
    user = await _get_message_user(message)
    if user is None:
        await state.clear()
        return
    try:
        job_id = int(data["job_id"])
        preset = str(data["preset"])
        mode = str(data["mode"])
        if preset == "free":
            raw_start, raw_end = [part.strip() for part in (message.text or "").split("-", 1)]
            start = parse_time(raw_start)
            end = parse_time(raw_end)
        else:
            start = parse_time((message.text or "").strip())
            end = None
        plan = await configure_and_queue_cut(
            job_id,
            start=start,
            end=end,
            preset=preset,
            mode=mode,
            quality="best",
        )
    except (KeyError, TypeError, ValueError, LookupError):
        text = (
            "Could not configure the cut. For Free Cut use 00:10 - 00:45; for 30/60 seconds send only the start time. Make sure enough media remains."
            if user.language == "en"
            else "تعذر ضبط القص. للقص الحر استخدم 00:10 - 00:45، ولـ30/60 ثانية أرسل وقت البداية فقط. تأكد أيضًا أن المدة المتبقية تكفي للمقطع المطلوب."
        )
        await message.answer(text)
        return
    await state.clear()
    progress = await message.answer(
        f"✂️ Job #{job_id} • QUEUED\n"
        f"Cut: {seconds_to_hms(plan.start)} → {seconds_to_hms(plan.end)} ({plan.duration:.0f}s)\n"
        f"Mode: {plan.mode}",
        reply_markup=_cancel_keyboard(job_id),
    )
    await _update_progress_message(job_id, progress.message_id)


@router.callback_query(F.data.startswith("cut:"))
async def upgrade_legacy_cut_button(callback: CallbackQuery) -> None:
    _, raw_id, mode = callback.data.split(":", 2)
    job_id = int(raw_id)
    owned = await _get_callback_job(callback, job_id)
    if owned is None:
        return
    job, user = owned
    if job.status != JobStatus.READY.value:
        await callback.answer("Job is not READY", show_alert=True)
        return
    text = (
        f"✂️ Cut options for Job #{job_id}\nChoose Free, 30s, or 60s, then provide the start/range."
        if user.language == "en"
        else f"✂️ خيارات القص لـ Job #{job_id}\nاختر المدة أو القص الحر، ثم حدد وقت البداية/النهاية."
    )
    await _edit_callback(callback, text, cut_menu_keyboard(job_id, mode))
    await callback.answer()


@router.callback_query(F.data.startswith("cutmenu:"))
async def show_cut_menu(callback: CallbackQuery) -> None:
    _, raw_id, mode = callback.data.split(":", 2)
    job_id = int(raw_id)
    owned = await _get_callback_job(callback, job_id)
    if owned is None:
        return
    job, user = owned
    if job.status != JobStatus.READY.value:
        await callback.answer("Job is not READY", show_alert=True)
        return
    if user.language == "en":
        text = f"✂️ Job #{job_id}\nDuration: {seconds_to_hms(job.duration)}\nChoose a cut type. Current mode: {mode.upper()}"
    else:
        text = f"✂️ Job #{job_id}\nالمدة: {seconds_to_hms(job.duration)}\nاختر نوع القص. الوضع الحالي: {mode.upper()}"
    await _edit_callback(callback, text, cut_menu_keyboard(job_id, mode))
    await callback.answer()


@router.callback_query(F.data.startswith("cutpick:"))
async def choose_cut_preset(callback: CallbackQuery, state: FSMContext) -> None:
    _, raw_id, preset, mode = callback.data.split(":", 3)
    job_id = int(raw_id)
    owned = await _get_callback_job(callback, job_id)
    if owned is None:
        return
    job, user = owned
    if job.status != JobStatus.READY.value:
        await callback.answer("Job is not READY", show_alert=True)
        return
    await state.set_state(FeatureState.waiting_cut_input)
    await state.update_data(job_id=job_id, preset=preset, mode=mode.upper())
    if user.language == "en":
        prompt = (
            "✂️ Free Cut: send start and end like 00:10 - 00:45"
            if preset == "free"
            else f"✂️ {preset}-second cut: send only the start time, e.g. 00:10"
        )
    else:
        prompt = (
            "✂️ قص حر: أرسل البداية والنهاية مثل 00:10 - 00:45"
            if preset == "free"
            else f"✂️ قص {preset} ثانية: أرسل وقت البداية فقط مثل 00:10"
        )
    await _edit_callback(callback, f"{prompt}\nMode: {mode.upper()}")
    await callback.answer()


@router.callback_query(F.data.startswith("cutback:"))
async def back_to_download_options(callback: CallbackQuery) -> None:
    job_id = int(callback.data.split(":", 1)[1])
    owned = await _get_callback_job(callback, job_id)
    if owned is None:
        return
    job, _ = owned
    if job.status != JobStatus.READY.value:
        await callback.answer("Job is not READY", show_alert=True)
        return
    async with SessionLocal() as session:
        metadata = await session.get(MediaMetadata, job_id)
        qualities = deserialize_qualities(metadata.formats_json if metadata else "[]")
    await _edit_callback(
        callback,
        f"Job #{job_id}\n{job.title or 'Media'}\nDuration: {seconds_to_hms(job.duration)}",
        media_keyboard(job_id, qualities, include_audio=True),
    )
    await callback.answer()


@router.message(ContainsMediaURL())
async def handle_media_text(message: Message) -> None:
    await _process_urls(message, intent="general")
