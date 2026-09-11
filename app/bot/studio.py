from __future__ import annotations

import asyncio
import json
import logging
import shutil
import time
from contextlib import suppress
from pathlib import Path

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError, TelegramRetryAfter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.bot.access import ensure_user, is_allowed
from app.bot.safe_edit import safe_edit_message
from app.bot.uploads import download_upload, upload_candidate
from app.config import get_settings
from app.db import MediaProject, ProjectStatus, RenderStatus, User
from app.errors import classify_error, user_error_message
from app.render_queue import cancel_render, enqueue_render
from app.services.ai_registry import get_ai_provider_registry
from app.services.assets import AssetService, asset_service
from app.services.composer import composer_service
from app.services.media import fit_media_for_upload, probe_media_file
from app.services.media_intelligence import MediaIntelligenceService
from app.services.projects import ProjectAssetItem, ProjectService, project_service
from app.services.render_service import RenderService, render_service
from app.services.studio_agent import StudioAgentService, studio_agent_service
from app.services.studio_session import (
    StudioPhase,
    StudioWorkflow,
    project_summary_text,
    recent_asset_context,
    transition_phase,
)
from app.services.timeline import timeline_service
from app.services.urls import parse_bulk_urls
from app.utils import seconds_to_hms

settings = get_settings()
router = Router(name="media-studio")
logger = logging.getLogger("moataz.studio.telegram")
_delivery_tasks: set[asyncio.Task] = set()
_analysis_tasks: set[asyncio.Task] = set()
_summary_tasks: dict[tuple[int, int], asyncio.Task] = {}


class StudioState(StatesGroup):
    collecting = State()
    collecting_media = State()
    waiting_instruction = State()
    agent = State()
    awaiting_feedback = State()


def studio_home_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🎬 مونتاج بالذكاء الاصطناعي", callback_data="studio:workflow:smart_edit")],
            [InlineKeyboardButton(text="✂️ قص وتقسيم", callback_data="studio:workflow:cut")],
            [InlineKeyboardButton(text="🎵 تعديل/استبدال الصوت", callback_data="studio:workflow:audio")],
            [InlineKeyboardButton(text="📝 ترجمة وكابتشن", callback_data="studio:workflow:captions")],
            [InlineKeyboardButton(text="🔄 تعديل فيديو سابق", callback_data="studio:reopen")],
            [InlineKeyboardButton(text="📂 مشاريعي", callback_data="studio:projects")],
            [InlineKeyboardButton(text="🏠 الرئيسية", callback_data="menu:home")],
        ]
    )


def collecting_keyboard(project_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ انتهيت من رفع المواد", callback_data=f"studio:done:{project_id}")],
            [
                InlineKeyboardButton(text="📋 ملخص المشروع", callback_data=f"studio:summary:{project_id}"),
                InlineKeyboardButton(text="📦 المواد", callback_data=f"studio:assets:{project_id}"),
            ],
            [InlineKeyboardButton(text="🗑 إلغاء المشروع", callback_data=f"studio:cancelproject:{project_id}")],
        ]
    )


def instruction_keyboard(project_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="⚡ Reels سريع", callback_data=f"studio:style:{project_id}:reels-fast"),
                InlineKeyboardButton(text="🎬 سينمائي", callback_data=f"studio:style:{project_id}:cinematic"),
            ],
            [
                InlineKeyboardButton(text="📢 إعلان", callback_data=f"studio:style:{project_id}:product-ad"),
                InlineKeyboardButton(text="🎙 Podcast", callback_data=f"studio:style:{project_id}:podcast"),
            ],
            [InlineKeyboardButton(text="✨ تلقائي بالكامل", callback_data=f"studio:style:{project_id}:auto")],
            [
                InlineKeyboardButton(text="➕ إضافة مواد", callback_data=f"studio:add:{project_id}"),
                InlineKeyboardButton(text="📋 ملخص المشروع", callback_data=f"studio:summary:{project_id}"),
            ],
        ]
    )


def cut_instruction_keyboard(project_id: int) -> InlineKeyboardMarkup:
    actions = [
        ("⏱ قص من وقت إلى وقت", "trim"),
        ("🗑 حذف جزء", "remove_range"),
        ("🧩 تقسيم كل 30 ثانية", "split_30"),
        ("🧩 تقسيم كل 60 ثانية", "split_60"),
        ("🎞 تقسيم حسب المشاهد", "split_scenes"),
        ("🔇 حذف الصمت", "remove_silence"),
        ("✨ استخراج أفضل جزء", "best_moment"),
    ]
    rows = [
        [
            InlineKeyboardButton(
                text=label, callback_data=f"studio:operation:{project_id}:{value}"
            )
        ]
        for label, value in actions
    ]
    rows.append(
        [InlineKeyboardButton(text="📋 ملخص المشروع", callback_data=f"studio:summary:{project_id}")]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def audio_instruction_keyboard(project_id: int) -> InlineKeyboardMarkup:
    actions = [
        ("🔇 إزالة الصوت الأصلي", "mute_original"),
        ("🔁 استبدال الصوت", "replace_audio"),
        ("🎚 خلط الصوت", "mix_audio"),
        ("🗣 Voice Over + Ducking", "auto_duck"),
        ("📏 Normalize", "normalize"),
    ]
    rows = [
        [
            InlineKeyboardButton(
                text=label, callback_data=f"studio:operation:{project_id}:{value}"
            )
        ]
        for label, value in actions
    ]
    rows.append(
        [InlineKeyboardButton(text="📋 ملخص المشروع", callback_data=f"studio:summary:{project_id}")]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def workflow_instruction_keyboard(
    project_id: int, workflow: str
) -> InlineKeyboardMarkup:
    if workflow == StudioWorkflow.CUT.value:
        return cut_instruction_keyboard(project_id)
    if workflow == StudioWorkflow.AUDIO.value:
        return audio_instruction_keyboard(project_id)
    return instruction_keyboard(project_id)


def project_keyboard(project_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="➕ إضافة مادة", callback_data=f"studio:add:{project_id}"),
                InlineKeyboardButton(text="📦 المواد", callback_data=f"studio:assets:{project_id}"),
            ],
            [
                InlineKeyboardButton(text="⚙️ الإعدادات", callback_data=f"studio:settings:{project_id}"),
                InlineKeyboardButton(text="✅ بدء المونتاج", callback_data=f"studio:start:{project_id}"),
            ],
            [InlineKeyboardButton(text="🤖 مونتاج بالذكاء الاصطناعي", callback_data=f"studio:agent:{project_id}")],
            [InlineKeyboardButton(text="🗑 إلغاء المشروع", callback_data=f"studio:cancelproject:{project_id}")],
            [InlineKeyboardButton(text="🏠 الرئيسية", callback_data="menu:home")],
        ]
    )


def agent_keyboard(project_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="👁 معاينة", callback_data=f"studio:agentrender:{project_id}:preview"),
                InlineKeyboardButton(text="🎬 تصدير نهائي", callback_data=f"studio:agentrender:{project_id}:final"),
            ],
            [
                InlineKeyboardButton(text="↩️ تراجع", callback_data=f"studio:agentundo:{project_id}"),
                InlineKeyboardButton(text="↪️ إعادة", callback_data=f"studio:agentredo:{project_id}"),
            ],
            [
                InlineKeyboardButton(text="📦 المواد", callback_data=f"studio:assets:{project_id}"),
                InlineKeyboardButton(text="📋 ملخص المشروع", callback_data=f"studio:summary:{project_id}"),
            ],
            [InlineKeyboardButton(text="➕ إضافة ملف", callback_data=f"studio:add:{project_id}")],
            [InlineKeyboardButton(text="⬅️ المشروع", callback_data=f"studio:open:{project_id}")],
        ]
    )


def render_cancel_keyboard(render_job_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✖️ إلغاء الرندر", callback_data=f"studio:cancelrender:{render_job_id}")]
        ]
    )


async def _message_user(message: Message) -> User | None:
    if message.from_user is None or not await is_allowed(message.from_user.id):
        return None
    return await ensure_user(message.from_user.id, message.from_user.username)


async def _callback_user(callback: CallbackQuery) -> User | None:
    if callback.from_user is None or not await is_allowed(callback.from_user.id):
        await callback.answer("غير متاح", show_alert=True)
        return None
    return await ensure_user(callback.from_user.id, callback.from_user.username)


async def _owned_project(callback: CallbackQuery, project_id: int) -> tuple[User, MediaProject] | None:
    user = await _callback_user(callback)
    if user is None:
        return None
    project = await project_service.get_project(project_id, user_id=user.id)
    if project is None:
        await callback.answer("المشروع غير موجود", show_alert=True)
        return None
    return user, project


def _project_text(project_id: int) -> str:
    return (
        f"🎬 مشروع #{project_id}\n\n"
        "أرسل المواد التي تريد استخدامها:\n\n"
        "🎥 فيديو  🖼 صورة  🎵 صوت  🎙 رسالة صوتية\n"
        "📄 ملف وسائط  🔗 رابط\n\n"
        "يمكنك إرسال أكثر من ملف أو رابط. بعد الانتهاء اضغط: ✅ بدء المونتاج"
    )


_WORKFLOW_PROMPTS = {
    StudioWorkflow.SMART_EDIT: (
        "أرسل الآن كل المواد التي تريد استخدامها في المونتاج.\n\n"
        "🎥 فيديوهات  🖼 صور  🎵 موسيقى\n"
        "🎙 أصوات  📄 ملفات وسائط  🔗 روابط\n\n"
        "لن يبدأ المونتاج أثناء الرفع. بعد إرسال جميع المواد اضغط: "
        "✅ انتهيت من رفع المواد"
    ),
    StudioWorkflow.CUT: (
        "✂️ أرسل الفيديو أو رابطه أولًا. بعد الانتهاء ستكتب عملية القص، مثل: "
        "«احذف أول 8 ثوان وآخر 4 ثوان» أو «قسمه حسب المشاهد»."
    ),
    StudioWorkflow.AUDIO: (
        "🎵 أرسل الفيديو ثم الصوت أو الموسيقى المطلوبة. بعد الانتهاء اكتب هل تريد "
        "الكتم أو الاستبدال أو الخلط أو Auto Ducking."
    ),
    StudioWorkflow.CAPTIONS: (
        "📝 أرسل الفيديو أو الصوت وملف الترجمة إن وجد. بعد الانتهاء صف لغة وشكل الكابتشن."
    ),
    StudioWorkflow.REOPEN: (
        "🔄 أرسل مواد جديدة للمشروع السابق، أو اضغط «انتهيت» للانتقال مباشرة إلى التعديلات."
    ),
}


async def _set_phase(
    state: FSMContext, target: StudioPhase, *, initial: bool = False
) -> StudioPhase:
    data = await state.get_data()
    raw_current = data.get("studio_phase")
    if initial or raw_current is None:
        phase = target
    else:
        phase = transition_phase(str(raw_current), target)
    await state.update_data(studio_phase=phase.value)
    return phase


async def _set_phase_if_session(
    state: FSMContext | None, target: StudioPhase
) -> None:
    if state is None:
        return
    data = await state.get_data()
    if not data.get("studio_phase"):
        return
    await _set_phase(state, target)


async def _project_summary(
    project_id: int,
    *,
    user_id: int,
    phase: str | StudioPhase,
) -> str:
    project = await project_service.get_project(project_id, user_id=user_id)
    if project is None:
        raise LookupError("Project not found")
    items = await project_service.list_assets(project_id, user_id=user_id)
    timeline = await timeline_service.get(project_id, user_id=user_id)
    return project_summary_text(project, items, timeline, phase=phase)


def _schedule_project_summary(
    message: Message,
    *,
    project_id: int,
    user_id: int,
    phase: StudioPhase = StudioPhase.COLLECTING_MEDIA,
) -> None:
    chat_id = int(getattr(getattr(message, "chat", None), "id", user_id))
    key = (chat_id, project_id)
    previous = _summary_tasks.pop(key, None)
    if previous is not None and not previous.done():
        previous.cancel()

    async def publish() -> None:
        try:
            await asyncio.sleep(1.2)
            text = await _project_summary(project_id, user_id=user_id, phase=phase)
            await message.answer(text, reply_markup=collecting_keyboard(project_id))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "studio summary update failed project_id=%s error_type=%s",
                project_id,
                type(exc).__name__,
            )
        finally:
            if _summary_tasks.get(key) is asyncio.current_task():
                _summary_tasks.pop(key, None)

    task = asyncio.create_task(publish(), name=f"studio-summary-{project_id}")
    _summary_tasks[key] = task


def _asset_name(item: ProjectAssetItem) -> str:
    with suppress(json.JSONDecodeError):
        metadata = json.loads(item.asset.metadata_json or "{}")
        name = metadata.get("original_name") or metadata.get("title")
        if name:
            return str(name).replace("\n", " ")[:42]
    return Path(item.asset.local_path).name[:42]


def _asset_icon(asset_type: str) -> str:
    return {
        "video": "🎥",
        "image": "🖼",
        "logo": "🏷",
        "audio": "🎵",
        "voice": "🎙",
        "subtitle": "💬",
    }.get(asset_type, "📄")


def _automatic_role(asset_type: str, file_name: str) -> str:
    normalized = file_name.casefold()
    if asset_type == "voice":
        return "voice"
    if asset_type == "audio":
        return "music" if any(
            value in normalized for value in ("music", "song", "track", "موسيقى")
        ) else "voice"
    if asset_type in {"image", "logo"} and any(
        value in normalized for value in ("logo", "شعار")
    ):
        return "logo"
    return "main"


async def _show_assets(message: Message, project_id: int, user_id: int) -> None:
    items = await project_service.list_assets(project_id, user_id=user_id)
    timeline = await timeline_service.get(project_id, user_id=user_id)
    clips = {
        str(clip.get("id")): clip
        for track in timeline.get("tracks") or []
        if isinstance(track, dict)
        for clip in track.get("clips") or []
        if isinstance(clip, dict)
    }
    lines = [f"📦 مواد Project #{project_id}"]
    rows: list[list[InlineKeyboardButton]] = []
    for index, item in enumerate(items, start=1):
        lines.append(
            f"{index}. {_asset_icon(item.asset.asset_type)} {_asset_name(item)} — {item.link.role}"
        )
        rows.append(
            [
                InlineKeyboardButton(text=f"⬆️ {index}", callback_data=f"studio:move:{project_id}:{item.asset.id}:u"),
                InlineKeyboardButton(text=f"⬇️ {index}", callback_data=f"studio:move:{project_id}:{item.asset.id}:d"),
                InlineKeyboardButton(text="🏷 الدور", callback_data=f"studio:rolemenu:{project_id}:{item.asset.id}"),
                InlineKeyboardButton(text="🗑", callback_data=f"studio:remove:{project_id}:{item.asset.id}"),
            ]
        )
        if item.asset.asset_type == "video":
            clip = clips.get(f"asset-{item.asset.id}")
            enabled = True if clip is None else clip.get("original_audio_enabled", True)
            action = "mute" if enabled else "unmute"
            label = "🔇 إزالة الصوت الأصلي" if enabled else "🔊 استعادة الصوت الأصلي"
            rows.append(
                [
                    InlineKeyboardButton(
                        text=label,
                        callback_data=f"studio:audio:{project_id}:{item.asset.id}:{action}",
                    )
                ]
            )
    if not items:
        lines.append("\nلا توجد مواد بعد.")
    rows.append([InlineKeyboardButton(text="⬅️ المشروع", callback_data=f"studio:open:{project_id}")])
    await _safe_bound_edit(
        message,
        "\n".join(lines),
        InlineKeyboardMarkup(inline_keyboard=rows),
    )


def _timeline(project: object) -> dict:
    try:
        payload = json.loads(str(getattr(project, "timeline_json", "{}")))
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


async def _apply_option(project_id: int, user_id: int, key: str, value: str) -> None:
    project = await project_service.get_project(project_id, user_id=user_id)
    if project is None:
        raise LookupError("Project not found")
    payload = _timeline(project)
    await project_service.apply_template(
        project_id,
        user_id=user_id,
        template=str(payload.get("template") or "auto"),
        options={key: value},
    )


async def _show_settings(message: Message, project_id: int, user_id: int) -> None:
    project = await project_service.get_project(project_id, user_id=user_id)
    if project is None:
        raise LookupError("Project not found")
    options = _timeline(project).get("options") or {}
    text = (
        f"⚙️ إعدادات Project #{project_id}\n"
        f"Aspect: {project.aspect_ratio}\n"
        f"Fit: {options.get('fit_mode', 'fit')}\n"
        f"Transition: {options.get('transition', 'none')}\n"
        f"Audio: {options.get('audio_mode', 'replace_audio')}\n"
        f"Logo: {options.get('logo_position', 'top-right')}"
    )
    rows = [
        [
            InlineKeyboardButton(text="📱 9:16", callback_data=f"studio:preset:{project_id}:vertical"),
            InlineKeyboardButton(text="🖥 16:9", callback_data=f"studio:preset:{project_id}:horizontal"),
            InlineKeyboardButton(text="⬜ 1:1", callback_data=f"studio:preset:{project_id}:square"),
        ],
        [
            InlineKeyboardButton(text="fit", callback_data=f"studio:set:{project_id}:fit_mode:fit"),
            InlineKeyboardButton(text="fill", callback_data=f"studio:set:{project_id}:fit_mode:fill"),
            InlineKeyboardButton(text="blur", callback_data=f"studio:set:{project_id}:fit_mode:blur-background"),
        ],
        [
            InlineKeyboardButton(text="Transition: none", callback_data=f"studio:set:{project_id}:transition:none"),
            InlineKeyboardButton(text="fade", callback_data=f"studio:set:{project_id}:transition:fade"),
            InlineKeyboardButton(text="dissolve", callback_data=f"studio:set:{project_id}:transition:dissolve"),
        ],
        [
            InlineKeyboardButton(text="slide", callback_data=f"studio:set:{project_id}:transition:slide"),
            InlineKeyboardButton(text="wipe", callback_data=f"studio:set:{project_id}:transition:wipe"),
            InlineKeyboardButton(text="zoom", callback_data=f"studio:set:{project_id}:transition:zoom"),
        ],
        [
            InlineKeyboardButton(text="blur", callback_data=f"studio:set:{project_id}:transition:blur"),
            InlineKeyboardButton(text="push", callback_data=f"studio:set:{project_id}:transition:push"),
            InlineKeyboardButton(text="dip black", callback_data=f"studio:set:{project_id}:transition:dip-to-black"),
        ],
        [
            InlineKeyboardButton(text="🔇 replace", callback_data=f"studio:set:{project_id}:audio_mode:replace_audio"),
            InlineKeyboardButton(text="🎚 mix", callback_data=f"studio:set:{project_id}:audio_mode:mix_audio"),
            InlineKeyboardButton(text="🎵 background", callback_data=f"studio:set:{project_id}:audio_mode:background_music"),
        ],
        [
            InlineKeyboardButton(text="↖", callback_data=f"studio:set:{project_id}:logo_position:top-left"),
            InlineKeyboardButton(text="↗", callback_data=f"studio:set:{project_id}:logo_position:top-right"),
            InlineKeyboardButton(text="↙", callback_data=f"studio:set:{project_id}:logo_position:bottom-left"),
            InlineKeyboardButton(text="↘", callback_data=f"studio:set:{project_id}:logo_position:bottom-right"),
            InlineKeyboardButton(text="⏺", callback_data=f"studio:set:{project_id}:logo_position:center"),
        ],
        [InlineKeyboardButton(text="⬅️ المشروع", callback_data=f"studio:open:{project_id}")],
    ]
    await _safe_bound_edit(
        message, text, InlineKeyboardMarkup(inline_keyboard=rows)
    )


def _template_choices(project_id: int, items: list[ProjectAssetItem]) -> InlineKeyboardMarkup:
    roles = {item.link.role for item in items}
    images = [item for item in items if item.asset.asset_type in {"image", "logo"} and item.link.role != "logo"]
    audios = [item for item in items if item.asset.asset_type in {"audio", "voice"}]
    videos = [item for item in items if item.asset.asset_type == "video"]
    logos = [item for item in items if item.link.role == "logo"]
    rows: list[list[InlineKeyboardButton]] = []
    if {"intro", "outro"}.issubset(roles) and videos:
        rows.append([InlineKeyboardButton(text="🎬 Intro + Main + Outro", callback_data=f"studio:render:{project_id}:intro_main_outro:replace_audio")])
    elif len(videos) == 1 and audios:
        rows.extend(
            [
                [InlineKeyboardButton(text="🔇 استبدال الصوت", callback_data=f"studio:render:{project_id}:video_audio:replace_audio")],
                [InlineKeyboardButton(text="🎚 خلط الصوت", callback_data=f"studio:render:{project_id}:video_audio:mix_audio")],
                [InlineKeyboardButton(text="🎵 موسيقى خلفية", callback_data=f"studio:render:{project_id}:video_audio:background_music")],
            ]
        )
    elif len(images) == 1 and len(audios) == 1 and not videos:
        rows.append([InlineKeyboardButton(text="🖼 صورة + صوت", callback_data=f"studio:render:{project_id}:audio_image:replace_audio")])
    elif images and not videos:
        rows.append([InlineKeyboardButton(text="🎞 Slideshow", callback_data=f"studio:render:{project_id}:slideshow:replace_audio")])
    elif videos and not audios:
        template = "logo_overlay" if len(videos) == 1 and logos else "merge_videos"
        rows.append([InlineKeyboardButton(text="🎬 دمج الفيديوهات", callback_data=f"studio:render:{project_id}:{template}:replace_audio")])
    rows.append([InlineKeyboardButton(text="⬅️ المشروع", callback_data=f"studio:open:{project_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _progress_text(project_id: int, progress: float, started: float) -> str:
    percent = max(0, min(100, int(progress * 100)))
    filled = min(10, percent // 10)
    elapsed = max(0.0, time.monotonic() - started)
    remaining = elapsed * (1 - progress) / progress if progress > 0.01 else None
    remaining_text = f"~{seconds_to_hms(remaining)}" if remaining is not None else "—"
    return (
        f"🎬 Rendering Project #{project_id}\n\n"
        f"{'█' * filled}{'░' * (10 - filled)} {percent}%\n\n"
        f"Elapsed: {seconds_to_hms(elapsed)}  Remaining: {remaining_text}"
    )


async def _safe_edit(bot: Bot, chat_id: int, message_id: int, text: str, markup=None) -> bool:
    try:
        await bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=text, reply_markup=markup)
        return True
    except TelegramRetryAfter as exc:
        await asyncio.sleep(min(float(exc.retry_after), 5.0))
        try:
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=text,
                reply_markup=markup,
            )
            return True
        except TelegramBadRequest as retry_exc:
            if "message is not modified" in str(retry_exc).lower():
                return True
        except (TelegramRetryAfter, TelegramNetworkError):
            return False
    except TelegramBadRequest as exc:
        # Telegram reports this as an error even though the requested UI state is
        # already visible. Treat it as success so delivery monitoring continues.
        if "message is not modified" in str(exc).lower():
            return True
    except TelegramNetworkError:
        return False
    return False


async def _safe_bound_edit(message: Message, text: str, markup=None) -> bool:
    """Edit a received/sent message without letting Telegram UI errors break work."""
    return await safe_edit_message(message, text, markup)


def _safe_studio_error(exc: Exception, *, agent: bool = False) -> str:
    """Return an actionable message without exposing SQL, paths, or credentials."""

    detail = str(exc).lower()
    if agent and ("clip_id" in detail or "clip_index" in detail):
        return "لم أستطع تحديد المقطع المقصود. اذكر رقمه، مثل: «قص المقطع الأول»."
    if agent and "requires a media clip" in detail:
        return "لا توجد مادة مناسبة لهذا التعديل. أرسل فيديوًا أو صوتًا إلى المشروع أولًا."
    if "project asset limit" in detail:
        return "وصل المشروع إلى الحد الأقصى المسموح من المواد."
    if "unsupported" in detail or "not valid for" in detail:
        return "نوع الملف أو العملية غير مدعوم في هذا المشروع."
    if "file size" in detail or "too large" in detail:
        return "حجم الملف يتجاوز الحد المسموح."
    if "duration" in detail and isinstance(exc, ValueError):
        return "مدة المادة أو التعديل تتجاوز الحدود المسموحة."
    if isinstance(exc, LookupError):
        return "المشروع أو المادة لم تعد متاحة."
    download_error = classify_error(exc)
    if download_error.code.value not in {"UNKNOWN", "DATABASE_ERROR", "STORAGE_ERROR"}:
        return user_error_message(download_error)
    if agent:
        return "تعذر تطبيق التعليمات بأمان. حاول صياغتها بخطوة واحدة مع تحديد رقم المقطع."
    return "تعذر قبول الملف بسبب خطأ داخلي مؤقت. حاول إرساله مرة أخرى."


async def watch_render_and_deliver(
    bot: Bot,
    *,
    chat_id: int,
    message_id: int,
    render_job_id: int,
    user_id: int,
    service: RenderService = render_service,
    state: FSMContext | None = None,
) -> None:
    started = time.monotonic()
    last_progress = -1.0
    while True:
        job = await service.get_render(render_job_id, user_id=user_id)
        if job is None:
            return
        if job.status in {RenderStatus.QUEUED.value, RenderStatus.PREPARING.value, RenderStatus.RENDERING.value}:
            if job.progress > last_progress:
                last_progress = job.progress
                await _safe_edit(
                    bot,
                    chat_id,
                    message_id,
                    _progress_text(job.project_id, job.progress, started),
                    render_cancel_keyboard(job.id),
                )
            await asyncio.sleep(max(1.2, settings.progress_update_seconds))
            continue
        if job.status == RenderStatus.CANCELLED.value:
            await _safe_edit(bot, chat_id, message_id, f"✖️ تم إلغاء Render #{job.id}")
            with suppress(ValueError):
                await _set_phase_if_session(state, StudioPhase.AWAITING_FEEDBACK)
            return
        if job.status == RenderStatus.FAILED.value:
            await _safe_edit(
                bot,
                chat_id,
                message_id,
                f"❌ فشل Render #{job.id}\n{(job.error or 'Unknown error')[:700]}",
                InlineKeyboardMarkup(
                    inline_keyboard=[[InlineKeyboardButton(text="🔁 إعادة الرندر", callback_data=f"studio:start:{job.project_id}")]]
                ),
            )
            with suppress(ValueError):
                await _set_phase_if_session(state, StudioPhase.AWAITING_FEEDBACK)
            return
        if job.status == RenderStatus.UPLOADING.value:
            await asyncio.sleep(max(1.2, settings.progress_update_seconds))
            continue
        if job.status != RenderStatus.COMPLETED.value or not job.output_path:
            return

        kind_getter = getattr(service, "get_render_kind", None)
        render_kind = await kind_getter(job.id) if kind_getter is not None else "final"
        delivery_root = settings.render_temp_dir / f"delivery-{job.id}"
        delivery_root.mkdir(parents=True, exist_ok=True)
        try:
            output = Path(job.output_path)
            probe = await probe_media_file(output)
            if not probe.has_video or probe.duration <= 0:
                raise ValueError("Rendered output validation failed before delivery")
            prepared = await fit_media_for_upload(
                output,
                settings.telegram_upload_limit_bytes,
                delivery_root,
                attempts=3,
            )
            await service.mark_uploading(job.id)
            await _safe_edit(bot, chat_id, message_id, f"📤 تم الرندر، جارٍ إرسال Project #{job.project_id}…")
            await bot.send_video(
                chat_id=chat_id,
                video=FSInputFile(prepared, filename=f"project-{job.project_id}.mp4"),
                caption=(
                    f"👁 معاينة Project #{job.project_id} • Render #{job.id}"
                    if render_kind == "preview"
                    else f"✅ Project #{job.project_id} • Render #{job.id}"
                ),
                supports_streaming=True,
            )
            await service.finish_delivery(job.id)
            await _safe_edit(
                bot,
                chat_id,
                message_id,
                (
                    f"👁 تم إرسال معاينة Project #{job.project_id}. يمكنك متابعة التعديل."
                    if render_kind == "preview"
                    else f"✅ اكتمل Project #{job.project_id} وتم إرسال الفيديو."
                ),
            )
            with suppress(ValueError):
                await _set_phase_if_session(
                    state,
                    StudioPhase.AWAITING_FEEDBACK
                    if render_kind == "preview"
                    else StudioPhase.COMPLETED,
                )
            if state is not None:
                await state.set_state(
                    StudioState.awaiting_feedback
                    if render_kind == "preview"
                    else None
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await service.finish_delivery(job.id, error=type(exc).__name__)
            with suppress(Exception):
                await bot.send_message(
                    chat_id,
                    "✅ تم إنشاء الفيديو بنجاح لكن تعذر إرساله عبر Telegram. "
                    "يمكنك فتح المشروع والمحاولة مجددًا.",
                )
            logger.warning("studio delivery failed render_job_id=%s error=%s", job.id, type(exc).__name__)
        finally:
            shutil.rmtree(delivery_root, ignore_errors=True)
        return


def _track(task: asyncio.Task) -> None:
    _delivery_tasks.add(task)
    task.add_done_callback(_delivery_tasks.discard)


def _schedule_asset_analysis(asset_id: int, *, user_id: int, project_id: int) -> None:
    """Analyze newly attached media without holding the Telegram update open."""

    service = MediaIntelligenceService(settings)
    task = asyncio.create_task(
        service.analyze_asset(asset_id, user_id=user_id, project_id=project_id),
        name=f"studio-analysis-{asset_id}",
    )
    _analysis_tasks.add(task)

    def completed(done: asyncio.Task) -> None:
        _analysis_tasks.discard(done)
        if done.cancelled():
            return
        if exc := done.exception():
            logger.warning(
                "studio media analysis failed asset_id=%s error_type=%s",
                asset_id,
                type(exc).__name__,
            )

    task.add_done_callback(completed)


async def _enqueue_agent_render(
    message: Message,
    project_id: int,
    user_id: int,
    kind: str,
    *,
    state: FSMContext | None = None,
) -> None:
    await _set_phase_if_session(
        state,
        StudioPhase.PREVIEW_RENDERING
        if kind == "preview"
        else StudioPhase.FINAL_RENDERING,
    )
    job = await render_service.create_render(
        project_id,
        user_id=user_id,
        kind=kind,
        preview_duration=15 if kind == "preview" else None,
        preview_width=480 if kind == "preview" else None,
    )
    await enqueue_render(job.id)
    progress = await message.answer(
        _progress_text(project_id, 0.0, time.monotonic()),
        reply_markup=render_cancel_keyboard(job.id),
    )
    _track(
        asyncio.create_task(
            watch_render_and_deliver(
                message.bot,
                chat_id=message.chat.id,
                message_id=progress.message_id,
                render_job_id=job.id,
                user_id=user_id,
                state=state,
            ),
            name=f"studio-agent-delivery-{job.id}",
        )
    )


@router.callback_query(F.data == "menu:studio")
async def studio_home(callback: CallbackQuery, state: FSMContext) -> None:
    user = await _callback_user(callback)
    if user is None:
        return
    await state.clear()
    if callback.message:
        await _safe_bound_edit(
            callback.message,
            "🎞 Media Studio\n\nاختر العملية التي تريد تنفيذها:",
            studio_home_keyboard(),
        )
    await callback.answer()


async def _begin_workflow(
    callback: CallbackQuery, state: FSMContext, workflow: StudioWorkflow
) -> None:
    user = await _callback_user(callback)
    if user is None or callback.message is None:
        return
    project = await project_service.create_project(
        user_id=user.id,
        chat_id=callback.message.chat.id,
        name={
            StudioWorkflow.SMART_EDIT: "AI Edit",
            StudioWorkflow.CUT: "Cut & Split",
            StudioWorkflow.AUDIO: "Audio Edit",
            StudioWorkflow.CAPTIONS: "Captions",
            StudioWorkflow.REOPEN: "Re-edit",
        }[workflow],
    )
    await state.set_state(StudioState.collecting_media)
    await state.update_data(
        studio_project_id=project.id,
        studio_workflow=workflow.value,
        studio_phase=StudioPhase.NEW.value,
        recent_asset_id=None,
    )
    await _set_phase(state, StudioPhase.COLLECTING_MEDIA)
    await _safe_bound_edit(
        callback.message,
        f"Project #{project.id}\n\n{_WORKFLOW_PROMPTS[workflow]}",
        collecting_keyboard(project.id),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("studio:workflow:"))
async def begin_workflow(callback: CallbackQuery, state: FSMContext) -> None:
    raw = callback.data.rsplit(":", 1)[1]
    try:
        workflow = StudioWorkflow(raw)
    except ValueError:
        await callback.answer("نوع العملية غير مدعوم", show_alert=True)
        return
    await _begin_workflow(callback, state, workflow)


@router.callback_query(F.data == "studio:new")
async def new_project(callback: CallbackQuery, state: FSMContext) -> None:
    await _begin_workflow(callback, state, StudioWorkflow.SMART_EDIT)


@router.callback_query(F.data.startswith("studio:open:"))
async def open_project(callback: CallbackQuery, state: FSMContext) -> None:
    project_id = int(callback.data.rsplit(":", 1)[1])
    owned = await _owned_project(callback, project_id)
    if owned is None or callback.message is None:
        return
    user, project = owned
    if project.status in {ProjectStatus.COMPLETED.value, ProjectStatus.FAILED.value}:
        try:
            project = await project_service.reopen_project(project_id, user_id=user.id)
        except ValueError as exc:
            await callback.answer(str(exc)[:180], show_alert=True)
            return
    if project.status not in {
        ProjectStatus.CANCELLED.value,
        ProjectStatus.RENDERING.value,
    }:
        data = await state.get_data()
        phase = str(data.get("studio_phase") or "")
        if int(data.get("studio_project_id") or 0) != project_id:
            phase = ""
        if phase in {
            StudioPhase.READY_FOR_INSTRUCTIONS.value,
            StudioPhase.ANALYZING.value,
            StudioPhase.PLANNING.value,
            StudioPhase.EDITING.value,
        }:
            await state.set_state(StudioState.waiting_instruction)
        elif phase in {
            StudioPhase.AWAITING_FEEDBACK.value,
            StudioPhase.PREVIEW_RENDERING.value,
        }:
            await state.set_state(StudioState.awaiting_feedback)
        else:
            await state.set_state(StudioState.collecting_media)
            await state.update_data(
                studio_project_id=project_id,
                studio_workflow=StudioWorkflow.REOPEN.value,
                studio_phase=StudioPhase.COLLECTING_MEDIA.value,
            )
    items = await project_service.list_assets(project_id, user_id=user.id)
    await _safe_bound_edit(
        callback.message,
        f"🎬 Project #{project_id} • {project.status}\nالمواد: {len(items)}\nAspect: {project.aspect_ratio}",
        project_keyboard(project_id),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("studio:add:"))
async def add_more(callback: CallbackQuery, state: FSMContext) -> None:
    project_id = int(callback.data.rsplit(":", 1)[1])
    owned = await _owned_project(callback, project_id)
    if owned is None or callback.message is None:
        return
    user, project = owned
    if project.status == ProjectStatus.RENDERING.value:
        await callback.answer("انتظر انتهاء الرندر الحالي أو ألغِه أولًا", show_alert=True)
        return
    if project.status in {ProjectStatus.COMPLETED.value, ProjectStatus.FAILED.value}:
        await project_service.reopen_project(project_id, user_id=user.id)
    data = await state.get_data()
    current = data.get("studio_phase")
    if current:
        try:
            await _set_phase(state, StudioPhase.COLLECTING_MEDIA)
        except ValueError:
            await callback.answer(
                "لا يمكن إضافة مواد أثناء العملية الحالية", show_alert=True
            )
            return
    else:
        await state.update_data(studio_phase=StudioPhase.COLLECTING_MEDIA.value)
    await state.set_state(StudioState.collecting_media)
    await state.update_data(studio_project_id=project_id)
    await _safe_bound_edit(
        callback.message,
        f"Project #{project_id}\n\n{_WORKFLOW_PROMPTS[StudioWorkflow.REOPEN]}",
        collecting_keyboard(project_id),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("studio:audio:"))
async def set_video_original_audio(callback: CallbackQuery) -> None:
    _, _, raw_project, raw_asset, action = callback.data.split(":")
    project_id = int(raw_project)
    asset_id = int(raw_asset)
    owned = await _owned_project(callback, project_id)
    if owned is None or callback.message is None or action not in {"mute", "unmute"}:
        return
    user, _ = owned
    items = await project_service.list_assets(project_id, user_id=user.id)
    if not any(item.asset.id == asset_id and item.asset.asset_type == "video" for item in items):
        await callback.answer("ملف الفيديو غير موجود في هذا المشروع", show_alert=True)
        return
    try:
        await timeline_service.apply(
            project_id,
            user_id=user.id,
            calls=[
                {
                    "name": "set_original_audio",
                    "arguments": {
                        "clip_id": f"asset-{asset_id}",
                        "enabled": action == "unmute",
                    },
                }
            ],
        )
    except ValueError as exc:
        await callback.answer(_safe_studio_error(exc, agent=True)[:180], show_alert=True)
        return
    await _show_assets(callback.message, project_id, user.id)
    await callback.answer("تم تحديث صوت الفيديو")


async def _show_agent_models(
    callback: CallbackQuery, state: FSMContext, project_id: int
) -> bool:
    if callback.message is None:
        return False
    registry = get_ai_provider_registry()
    if not registry.enabled:
        await callback.answer("لا يوجد مزود AI مفعّل.", show_alert=True)
        return False
    models, errors = await registry.models_for("text")
    models = models[:12]
    if not models:
        detail = next(iter(errors.values()), "لا توجد نماذج نصية متاحة")
        await callback.answer(detail[:180], show_alert=True)
        return False
    await state.update_data(
        studio_project_id=project_id,
        studio_agent_models=[model.state_dict() for model in models],
    )
    rows = [
        [
            InlineKeyboardButton(
                text=f"{model.provider_name} · {model.model_id}"[:60],
                callback_data=f"studio:agentmodel:{project_id}:{index}",
            )
        ]
        for index, model in enumerate(models)
    ]
    rows.append(
        [InlineKeyboardButton(text="⬅️ المشروع", callback_data=f"studio:open:{project_id}")]
    )
    await _safe_bound_edit(
        callback.message,
        f"✅ اكتمل جمع مواد Project #{project_id}.\n\n"
        "اختر نموذج التخطيط. سيقرأ ملخص المواد والتحليل المحفوظ، ولن يشغّل FFmpeg أو shell مباشرة:",
        InlineKeyboardMarkup(inline_keyboard=rows),
    )
    return True


@router.callback_query(F.data.startswith("studio:done:"))
async def finish_collecting(callback: CallbackQuery, state: FSMContext) -> None:
    project_id = int(callback.data.rsplit(":", 1)[1])
    owned = await _owned_project(callback, project_id)
    if owned is None or callback.message is None:
        return
    user, _ = owned
    items = await project_service.list_assets(project_id, user_id=user.id)
    if not items:
        await callback.answer("أرسل مادة واحدة على الأقل أولًا", show_alert=True)
        return
    try:
        await _set_phase(state, StudioPhase.READY_FOR_INSTRUCTIONS)
    except ValueError:
        await state.update_data(studio_phase=StudioPhase.READY_FOR_INSTRUCTIONS.value)
    await state.set_state(StudioState.waiting_instruction)
    if await _show_agent_models(callback, state, project_id):
        await callback.answer("تم حفظ المواد")


@router.callback_query(F.data.startswith("studio:agent:"))
async def open_agent(callback: CallbackQuery, state: FSMContext) -> None:
    project_id = int(callback.data.rsplit(":", 1)[1])
    owned = await _owned_project(callback, project_id)
    if owned is None or callback.message is None:
        return
    data = await state.get_data()
    if not data.get("studio_phase"):
        await state.update_data(
            studio_phase=StudioPhase.READY_FOR_INSTRUCTIONS.value,
            studio_workflow=StudioWorkflow.SMART_EDIT.value,
        )
    if await _show_agent_models(callback, state, project_id):
        await callback.answer()


@router.callback_query(F.data.startswith("studio:agentmodel:"))
async def choose_agent_model(callback: CallbackQuery, state: FSMContext) -> None:
    _, _, raw_project, raw_index = callback.data.split(":")
    project_id = int(raw_project)
    if await _owned_project(callback, project_id) is None or callback.message is None:
        return
    data = await state.get_data()
    models = list(data.get("studio_agent_models") or [])
    try:
        selected = models[int(raw_index)]
        provider_id = str(selected["provider_id"])
        model = str(selected["model_id"])
        capabilities = set(selected.get("capabilities") or [])
        input_modalities = set(selected.get("input_modalities") or [])
        native_tools = "tools" in capabilities
        vision = "vision" in capabilities or "image" in input_modalities
    except (IndexError, KeyError, TypeError, ValueError):
        await callback.answer("أعد فتح قائمة النماذج", show_alert=True)
        return
    await state.set_state(StudioState.waiting_instruction)
    await state.update_data(
        studio_project_id=project_id,
        studio_agent_provider_id=provider_id,
        studio_agent_model=model,
        studio_agent_native_tools=native_tools,
        studio_agent_vision=vision,
    )
    workflow = str(
        data.get("studio_workflow") or StudioWorkflow.SMART_EDIT.value
    )
    workflow_prompt = {
        StudioWorkflow.CUT.value: "اختر عملية القص أو اكتبها مباشرة مع التوقيت.",
        StudioWorkflow.AUDIO.value: "اختر معالجة الصوت أو صفها مباشرة.",
        StudioWorkflow.CAPTIONS.value: "صف لغة الكابتشن وتصميمه وتوقيته.",
        StudioWorkflow.REOPEN.value: "صف التعديل المطلوب على Timeline السابق.",
    }.get(
        workflow,
        "اكتب النتيجة التي تريدها بلغتك الطبيعية. مثال:\n"
        "«حلل الفيديو الرئيسي، احذف الصمت، استخدم الصور في الأماكن المناسبة، "
        "واصنع Reels سريعًا 30 ثانية مع كابتشن عربي.»",
    )
    await _safe_bound_edit(
        callback.message,
        f"🤖 Project #{project_id} جاهز للتعليمات\n\n{workflow_prompt}",
        workflow_instruction_keyboard(project_id, workflow),
    )
    await callback.answer("تم اختيار النموذج")


@router.callback_query(F.data.startswith("studio:operation:"))
async def select_workflow_operation(
    callback: CallbackQuery, state: FSMContext
) -> None:
    _, _, raw_project, operation = callback.data.split(":", 3)
    project_id = int(raw_project)
    if await _owned_project(callback, project_id) is None or callback.message is None:
        return
    prompts = {
        "trim": "اكتب وقت البداية والنهاية، مثل: «احتفظ من 00:08 إلى 00:42».",
        "remove_range": "حدد الجزء المحذوف، مثل: «احذف من 00:08 إلى 00:12».",
        "split_30": "اكتب «نفّذ» للتقسيم إلى مقاطع كل منها 30 ثانية.",
        "split_60": "اكتب «نفّذ» للتقسيم إلى مقاطع كل منها 60 ثانية.",
        "split_scenes": "اكتب «نفّذ» ليستخدم التحليل حدود المشاهد.",
        "remove_silence": "اكتب «نفّذ» لحذف فترات الصمت المكتشفة.",
        "best_moment": "حدد المدة المطلوبة لأفضل جزء، مثل: «أفضل 30 ثانية».",
        "mute_original": "اكتب «نفّذ» لكتم أصوات الفيديوهات الأصلية.",
        "replace_audio": "حدد الصوت المرفوع، مثل: «استبدله بالصوت الأخير».",
        "mix_audio": "صف مستويات الخلط المطلوبة.",
        "auto_duck": "اكتب «نفّذ» لخفض الموسيقى تلقائيًا أثناء الكلام.",
        "normalize": "اكتب «نفّذ» لموازنة مستوى الصوت.",
    }
    if operation not in prompts:
        await callback.answer("العملية غير مدعومة", show_alert=True)
        return
    await state.update_data(studio_operation=operation)
    data = await state.get_data()
    workflow = str(data.get("studio_workflow") or StudioWorkflow.SMART_EDIT.value)
    await _safe_bound_edit(
        callback.message,
        f"Project #{project_id}\n\n{prompts[operation]}",
        workflow_instruction_keyboard(project_id, workflow),
    )
    await callback.answer("تم اختيار العملية")


@router.message(StudioState.agent, F.video | F.photo | F.audio | F.voice | F.document)
async def receive_agent_upload(message: Message, state: FSMContext) -> None:
    await ingest_upload_message(message, state)


async def handle_agent_instruction(
    message: Message,
    state: FSMContext,
    *,
    agent: StudioAgentService = studio_agent_service,
) -> None:
    user = await _message_user(message)
    if user is None or not message.text:
        return
    data = await state.get_data()
    project_id = int(data.get("studio_project_id") or 0)
    if await project_service.get_project(project_id, user_id=user.id) is None:
        await state.clear()
        await message.answer("المشروع غير متاح.")
        return
    parsed = parse_bulk_urls(message.text, limit=settings.max_bulk_urls)
    stripped_lines = [line.strip() for line in message.text.splitlines() if line.strip()]
    if parsed.urls and len(parsed.urls) == len(stripped_lines):
        await receive_project_url(message, state)
        return
    provider_id = str(data.get("studio_agent_provider_id") or "")
    model = str(data.get("studio_agent_model") or "")
    if not provider_id or not model:
        await message.answer("أعد فتح وضع الذكاء الاصطناعي واختر نموذجًا.")
        return
    phase_before = str(
        data.get("studio_phase") or StudioPhase.READY_FOR_INSTRUCTIONS.value
    )
    if phase_before in {
        StudioPhase.PREVIEW_RENDERING.value,
        StudioPhase.FINAL_RENDERING.value,
    }:
        await message.answer("الرندر جارٍ الآن. انتظر اكتماله أو ألغِه قبل تعديل Timeline.")
        return
    items = await project_service.list_assets(project_id, user_id=user.id)
    recent = recent_asset_context(items, data.get("recent_asset_id"))
    workflow = str(data.get("studio_workflow") or StudioWorkflow.SMART_EDIT.value)
    selected_style = str(data.get("studio_style") or "")
    operation = str(data.get("studio_operation") or "")
    instruction = message.text
    operation_directives = {
        "trim": "نفّذ قصًا زمنيًا مطابقًا للتوقيت الذي يذكره المستخدم.",
        "remove_range": "احذف النطاق الزمني الذي يحدده المستخدم.",
        "split_30": "قسّم المادة بصريًا عند كل 30 ثانية.",
        "split_60": "قسّم المادة بصريًا عند كل 60 ثانية.",
        "split_scenes": "استخدم حدود المشاهد المحفوظة لتقسيم المقاطع.",
        "remove_silence": "استخدم تحليل الصمت المحفوظ واحذف نطاقاته.",
        "best_moment": "اختر أعلى اللحظات تقييمًا ضمن المدة المطلوبة.",
        "mute_original": "عطّل الصوت الأصلي لكل مقاطع الفيديو.",
        "replace_audio": "استبدل الصوت الأصلي بالصوت الذي يحدده المستخدم.",
        "mix_audio": "اخلط المصادر الصوتية بالمستويات التي يحددها المستخدم.",
        "auto_duck": "اخفض الموسيقى أثناء مقاطع الكلام أو Voice Over.",
        "normalize": "طبّق loudness normalization على مصادر الصوت.",
    }
    if operation:
        directive = operation_directives.get(operation)
        if directive:
            instruction = f"{directive} {instruction}"
    if selected_style:
        instruction = f"استخدم أسلوب {selected_style}. {instruction}"
    thinking = await message.answer("🔎 أفهم المشروع والمواد…")

    async def stage_changed(raw_phase: str) -> None:
        phase = StudioPhase(raw_phase)
        await _set_phase_if_session(state, phase)
        labels = {
            StudioPhase.ANALYZING: "🔎 أحلل محتوى المواد…",
            StudioPhase.PLANNING: "🧠 أبني خطة المونتاج…",
            StudioPhase.EDITING: "🛠 أطبق الخطة على Timeline…",
        }
        await _safe_bound_edit(thinking, labels[phase], agent_keyboard(project_id))

    try:
        reply = await agent.handle(
            project_id,
            user_id=user.id,
            instruction=instruction,
            provider_id=provider_id,
            model=model,
            native_tools=bool(data.get("studio_agent_native_tools")),
            vision=bool(data.get("studio_agent_vision")),
            project_context={
                "workflow": workflow,
                "selected_operation": operation or None,
                "recent_asset": recent,
                "project_asset_count": len(items),
            },
            stage_callback=stage_changed,
        )
        tools = "، ".join(reply.applied_tools)
        await _safe_bound_edit(
            thinking,
            f"✅ {reply.text[:1000]}\n\nالأدوات: {tools[:600]}",
            agent_keyboard(project_id),
        )
        if operation:
            await state.update_data(studio_operation=None)
        if reply.render_action:
            await _enqueue_agent_render(
                message, project_id, user.id, reply.render_action, state=state
            )
            await state.set_state(StudioState.awaiting_feedback)
        elif phase_before == StudioPhase.READY_FOR_INSTRUCTIONS.value:
            await _enqueue_agent_render(
                message, project_id, user.id, "preview", state=state
            )
            await state.set_state(StudioState.awaiting_feedback)
        else:
            await _set_phase_if_session(state, StudioPhase.AWAITING_FEEDBACK)
            await state.set_state(StudioState.awaiting_feedback)
    except Exception as exc:
        logger.exception(
            "studio agent request failed project_id=%s error_type=%s",
            project_id,
            type(exc).__name__,
        )
        await _safe_bound_edit(
            thinking,
            f"تعذر تطبيق التعليمات بأمان: {_safe_studio_error(exc, agent=True)}",
            agent_keyboard(project_id),
        )
        with suppress(ValueError):
            await _set_phase_if_session(state, StudioPhase.AWAITING_FEEDBACK)
        await state.set_state(StudioState.awaiting_feedback)


@router.message(StudioState.agent, F.text)
@router.message(StudioState.waiting_instruction, F.text)
@router.message(StudioState.awaiting_feedback, F.text)
async def receive_agent_instruction(message: Message, state: FSMContext) -> None:
    await handle_agent_instruction(message, state)


@router.callback_query(F.data.startswith("studio:agentrender:"))
async def agent_render(callback: CallbackQuery, state: FSMContext) -> None:
    _, _, raw_project, kind = callback.data.split(":")
    project_id = int(raw_project)
    owned = await _owned_project(callback, project_id)
    if owned is None or callback.message is None or kind not in {"preview", "final"}:
        return
    user, _ = owned
    try:
        await _enqueue_agent_render(
            callback.message, project_id, user.id, kind, state=state
        )
        await state.set_state(StudioState.awaiting_feedback)
    except Exception as exc:
        await callback.answer(str(exc)[:180], show_alert=True)
        return
    await callback.answer("بدأت المعاينة" if kind == "preview" else "بدأ التصدير")


@router.callback_query(F.data.startswith("studio:agentundo:"))
async def agent_undo(callback: CallbackQuery) -> None:
    project_id = int(callback.data.rsplit(":", 1)[1])
    owned = await _owned_project(callback, project_id)
    if owned is None or callback.message is None:
        return
    user, _ = owned
    try:
        await studio_agent_service.timelines.undo(project_id, user_id=user.id)
    except ValueError as exc:
        await callback.answer(str(exc), show_alert=True)
        return
    await callback.message.answer("↩️ تم التراجع عن آخر تعديل.", reply_markup=agent_keyboard(project_id))
    await callback.answer()


@router.callback_query(F.data.startswith("studio:agentredo:"))
async def agent_redo(callback: CallbackQuery) -> None:
    project_id = int(callback.data.rsplit(":", 1)[1])
    owned = await _owned_project(callback, project_id)
    if owned is None or callback.message is None:
        return
    user, _ = owned
    try:
        await studio_agent_service.timelines.redo(project_id, user_id=user.id)
    except ValueError as exc:
        await callback.answer(str(exc), show_alert=True)
        return
    await callback.message.answer("↪️ تمت إعادة التعديل.", reply_markup=agent_keyboard(project_id))
    await callback.answer()


async def ingest_upload_message(
    message: Message,
    state: FSMContext,
    *,
    assets: AssetService = asset_service,
    projects: ProjectService = project_service,
) -> None:
    user = await _message_user(message)
    candidate = upload_candidate(message)
    if user is None or candidate is None:
        return
    data = await state.get_data()
    project_id = int(data.get("studio_project_id") or 0)
    project = await projects.get_project(project_id, user_id=user.id)
    if project is None or project.status == ProjectStatus.CANCELLED.value:
        await state.clear()
        await message.answer("المشروع غير متاح.")
        return
    temporary: Path | None = None
    asset = None
    try:
        temporary = await download_upload(message, candidate, settings=settings)
        asset = await assets.ingest_file(
            temporary,
            user_id=user.id,
            project_id=project_id,
            declared_type=candidate.declared_type,
            source_type="telegram",
            mime_type=candidate.mime_type,
            telegram_file_id=candidate.file_id,
            metadata={"original_name": candidate.file_name},
        )
        role = _automatic_role(asset.asset_type, candidate.file_name)
        await projects.add_asset(
            project_id, asset.id, user_id=user.id, role=role
        )
        await state.update_data(
            recent_asset_id=asset.id,
            recent_asset_type=asset.asset_type,
            recent_asset_name=candidate.file_name[:255],
        )
        _schedule_asset_analysis(asset.id, user_id=user.id, project_id=project_id)
        phase = str(data.get("studio_phase") or StudioPhase.COLLECTING_MEDIA.value)
        if phase == StudioPhase.COLLECTING_MEDIA.value:
            _schedule_project_summary(
                message, project_id=project_id, user_id=user.id
            )
        else:
            await message.answer(
                f"{_asset_icon(asset.asset_type)} تمت إضافة {candidate.file_name[:60]} إلى Project #{project_id}.\n"
                "يمكنك الإشارة إليه الآن بقول: «الملف الذي أرسلته الآن».",
                reply_markup=agent_keyboard(project_id),
            )
    except Exception as exc:
        if asset is not None:
            with suppress(Exception):
                await assets.delete_unattached_asset(asset.id, user_id=user.id)
        logger.exception(
            "studio upload ingestion failed project_id=%s error_type=%s",
            project_id,
            type(exc).__name__,
        )
        await message.answer(f"تعذر قبول الملف: {_safe_studio_error(exc)}")
    finally:
        if temporary is not None:
            shutil.rmtree(temporary.parent, ignore_errors=True)


@router.message(
    StudioState.collecting,
    F.video | F.photo | F.audio | F.voice | F.document,
)
@router.message(
    StudioState.collecting_media,
    F.video | F.photo | F.audio | F.voice | F.document,
)
@router.message(
    StudioState.waiting_instruction,
    F.video | F.photo | F.audio | F.voice | F.document,
)
@router.message(
    StudioState.awaiting_feedback,
    F.video | F.photo | F.audio | F.voice | F.document,
)
async def receive_upload(message: Message, state: FSMContext) -> None:
    await ingest_upload_message(message, state)


@router.message(StudioState.collecting, F.text)
@router.message(StudioState.collecting_media, F.text)
async def receive_project_url(message: Message, state: FSMContext) -> None:
    user = await _message_user(message)
    if user is None:
        return
    data = await state.get_data()
    project_id = int(data.get("studio_project_id") or 0)
    project = await project_service.get_project(project_id, user_id=user.id)
    if project is None:
        await state.clear()
        return
    current = await project_service.list_assets(project_id, user_id=user.id)
    remaining = settings.max_project_assets - len(current)
    parsed = parse_bulk_urls(message.text or "", limit=max(1, min(settings.max_bulk_urls, remaining)))
    if not parsed.urls:
        await message.answer("أرسل ملف وسائط أو رابط HTTP/HTTPS صالحًا.")
        return
    progress = await message.answer(f"🔗 جارٍ إضافة {len(parsed.urls)} رابط…")
    added = 0
    failures: list[str] = []
    for url in parsed.urls:
        asset = None
        try:
            asset = await asset_service.ingest_url(url, user_id=user.id, project_id=project_id)
            await project_service.add_asset(
                project_id,
                asset.id,
                user_id=user.id,
                role=_automatic_role(asset.asset_type, ""),
            )
            await state.update_data(
                recent_asset_id=asset.id,
                recent_asset_type=asset.asset_type,
                recent_asset_name=f"URL #{asset.id}",
            )
            _schedule_asset_analysis(asset.id, user_id=user.id, project_id=project_id)
            added += 1
        except Exception as exc:
            if asset is not None:
                with suppress(Exception):
                    await asset_service.delete_unattached_asset(asset.id, user_id=user.id)
            failures.append(_safe_studio_error(exc))
    text = f"✅ تمت إضافة {added} رابط إلى Project #{project_id}."
    if failures:
        text += f"\nتعذر {len(failures)}: {failures[0]}"
    phase = str(data.get("studio_phase") or StudioPhase.COLLECTING_MEDIA.value)
    collecting = phase == StudioPhase.COLLECTING_MEDIA.value
    await _safe_bound_edit(
        progress,
        text,
        collecting_keyboard(project_id) if collecting else agent_keyboard(project_id),
    )
    if added and collecting:
        _schedule_project_summary(message, project_id=project_id, user_id=user.id)


@router.callback_query(F.data.startswith("studio:assets:"))
async def list_assets(callback: CallbackQuery) -> None:
    project_id = int(callback.data.rsplit(":", 1)[1])
    owned = await _owned_project(callback, project_id)
    if owned is None or callback.message is None:
        return
    user, _ = owned
    await _show_assets(callback.message, project_id, user.id)
    await callback.answer()


@router.callback_query(F.data.startswith("studio:move:"))
async def move_asset(callback: CallbackQuery) -> None:
    _, _, raw_project, raw_asset, direction = callback.data.split(":")
    project_id, asset_id = int(raw_project), int(raw_asset)
    owned = await _owned_project(callback, project_id)
    if owned is None or callback.message is None:
        return
    user, _ = owned
    await project_service.move_asset(
        project_id, asset_id, user_id=user.id, direction=-1 if direction == "u" else 1
    )
    await _show_assets(callback.message, project_id, user.id)
    await callback.answer()


@router.callback_query(F.data.startswith("studio:remove:"))
async def remove_asset(callback: CallbackQuery) -> None:
    _, _, raw_project, raw_asset = callback.data.split(":")
    project_id, asset_id = int(raw_project), int(raw_asset)
    owned = await _owned_project(callback, project_id)
    if owned is None or callback.message is None:
        return
    user, _ = owned
    await project_service.remove_asset(project_id, asset_id, user_id=user.id)
    await _show_assets(callback.message, project_id, user.id)
    await callback.answer()


@router.callback_query(F.data.startswith("studio:rolemenu:"))
async def role_menu(callback: CallbackQuery) -> None:
    _, _, raw_project, raw_asset = callback.data.split(":")
    project_id, asset_id = int(raw_project), int(raw_asset)
    if await _owned_project(callback, project_id) is None or callback.message is None:
        return
    roles = ["main", "intro", "outro", "music", "voice", "logo", "background"]
    rows = [
        [InlineKeyboardButton(text=role, callback_data=f"studio:role:{project_id}:{asset_id}:{role}")]
        for role in roles
    ]
    await _safe_bound_edit(
        callback.message,
        "🏷 اختر دور المادة:",
        InlineKeyboardMarkup(inline_keyboard=rows),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("studio:role:"))
async def set_role(callback: CallbackQuery) -> None:
    _, _, raw_project, raw_asset, role = callback.data.split(":")
    project_id, asset_id = int(raw_project), int(raw_asset)
    owned = await _owned_project(callback, project_id)
    if owned is None or callback.message is None:
        return
    user, _ = owned
    try:
        await project_service.set_role(project_id, asset_id, user_id=user.id, role=role)
    except ValueError as exc:
        await callback.answer(str(exc), show_alert=True)
        return
    await _show_assets(callback.message, project_id, user.id)
    await callback.answer()


@router.callback_query(F.data.startswith("studio:settings:"))
async def settings_menu(callback: CallbackQuery) -> None:
    project_id = int(callback.data.rsplit(":", 1)[1])
    owned = await _owned_project(callback, project_id)
    if owned is None or callback.message is None:
        return
    user, _ = owned
    await _show_settings(callback.message, project_id, user.id)
    await callback.answer()


@router.callback_query(F.data.startswith("studio:preset:"))
async def set_preset(callback: CallbackQuery) -> None:
    _, _, raw_project, preset = callback.data.split(":")
    project_id = int(raw_project)
    owned = await _owned_project(callback, project_id)
    if owned is None or callback.message is None:
        return
    user, _ = owned
    await project_service.set_preset(project_id, user_id=user.id, preset=preset)
    await _show_settings(callback.message, project_id, user.id)
    await callback.answer("تم الحفظ")


@router.callback_query(F.data.startswith("studio:set:"))
async def set_option(callback: CallbackQuery) -> None:
    _, _, raw_project, key, value = callback.data.split(":", 4)
    project_id = int(raw_project)
    owned = await _owned_project(callback, project_id)
    if owned is None or callback.message is None:
        return
    user, _ = owned
    await _apply_option(project_id, user.id, key, value)
    await _show_settings(callback.message, project_id, user.id)
    await callback.answer("تم الحفظ")


@router.callback_query(F.data.startswith("studio:start:"))
async def choose_template(callback: CallbackQuery) -> None:
    project_id = int(callback.data.rsplit(":", 1)[1])
    owned = await _owned_project(callback, project_id)
    if owned is None or callback.message is None:
        return
    user, _ = owned
    items = await project_service.list_assets(project_id, user_id=user.id)
    markup = _template_choices(project_id, items)
    if len(markup.inline_keyboard) == 1:
        await callback.answer("تركيبة المواد غير مدعومة", show_alert=True)
        return
    await _safe_bound_edit(callback.message, "🎬 اختر طريقة المونتاج:", markup)
    await callback.answer()


@router.callback_query(F.data.startswith("studio:render:"))
async def start_render(callback: CallbackQuery, state: FSMContext) -> None:
    _, _, raw_project, template, audio_mode = callback.data.split(":", 4)
    project_id = int(raw_project)
    owned = await _owned_project(callback, project_id)
    if owned is None or callback.message is None:
        return
    user, _ = owned
    try:
        await project_service.apply_template(
            project_id,
            user_id=user.id,
            template=template,
            options={"audio_mode": audio_mode},
        )
        await composer_service.build(project_id, user_id=user.id)
        job = await render_service.create_render(project_id, user_id=user.id)
        await enqueue_render(job.id)
    except Exception as exc:
        await callback.answer(str(exc)[:180], show_alert=True)
        return
    await state.clear()
    await _safe_bound_edit(
        callback.message,
        _progress_text(project_id, 0.0, time.monotonic()),
        render_cancel_keyboard(job.id),
    )
    _track(
        asyncio.create_task(
            watch_render_and_deliver(
                callback.bot,
                chat_id=callback.message.chat.id,
                message_id=callback.message.message_id,
                render_job_id=job.id,
                user_id=user.id,
            ),
            name=f"studio-delivery-{job.id}",
        )
    )
    await callback.answer("بدأ الرندر")


@router.callback_query(F.data.startswith("studio:cancelrender:"))
async def cancel_active_render(callback: CallbackQuery) -> None:
    render_id = int(callback.data.rsplit(":", 1)[1])
    user = await _callback_user(callback)
    if user is None:
        return
    cancelled = await cancel_render(render_id, user_id=user.id)
    await callback.answer("تم طلب الإلغاء" if cancelled else "الرندر منتهٍ أو غير موجود", show_alert=not cancelled)


@router.callback_query(F.data.startswith("studio:cancelproject:"))
async def cancel_project(callback: CallbackQuery, state: FSMContext) -> None:
    project_id = int(callback.data.rsplit(":", 1)[1])
    owned = await _owned_project(callback, project_id)
    if owned is None:
        return
    user, _ = owned
    cancelled = await project_service.cancel_project(project_id, user_id=user.id)
    if not cancelled:
        await callback.answer("لا يمكن إلغاء مشروع مكتمل أو ملغى", show_alert=True)
        return
    await render_service.cancel_project_renders(project_id, user_id=user.id)
    await state.clear()
    if callback.message:
        await _safe_bound_edit(
            callback.message, "🗑 تم إلغاء المشروع.", studio_home_keyboard()
        )
    await callback.answer()


@router.callback_query(F.data.startswith("studio:summary:"))
async def show_project_summary(callback: CallbackQuery, state: FSMContext) -> None:
    project_id = int(callback.data.rsplit(":", 1)[1])
    owned = await _owned_project(callback, project_id)
    if owned is None or callback.message is None:
        return
    user, _ = owned
    data = await state.get_data()
    phase = str(
        data.get("studio_phase") or StudioPhase.READY_FOR_INSTRUCTIONS.value
    )
    text = await _project_summary(project_id, user_id=user.id, phase=phase)
    markup = (
        collecting_keyboard(project_id)
        if phase == StudioPhase.COLLECTING_MEDIA.value
        else agent_keyboard(project_id)
    )
    await _safe_bound_edit(callback.message, text, markup)
    await callback.answer()


@router.callback_query(F.data.startswith("studio:style:"))
async def select_instruction_style(callback: CallbackQuery, state: FSMContext) -> None:
    _, _, raw_project, style = callback.data.split(":", 3)
    project_id = int(raw_project)
    if await _owned_project(callback, project_id) is None or callback.message is None:
        return
    allowed = {"reels-fast", "cinematic", "product-ad", "podcast", "auto"}
    if style not in allowed:
        await callback.answer("الأسلوب غير مدعوم", show_alert=True)
        return
    await state.update_data(studio_style="" if style == "auto" else style)
    prompt = (
        "✨ سيختار المخطط الأسلوب تلقائيًا. اكتب هدف الفيديو ومدته."
        if style == "auto"
        else f"✅ تم اختيار أسلوب {style}. اكتب الآن تفاصيل النتيجة المطلوبة."
    )
    await _safe_bound_edit(callback.message, prompt, instruction_keyboard(project_id))
    await callback.answer("تم اختيار الأسلوب")


@router.callback_query(F.data == "studio:reopen")
async def choose_project_to_reopen(callback: CallbackQuery) -> None:
    user = await _callback_user(callback)
    if user is None or callback.message is None:
        return
    projects = [
        project
        for project in await project_service.list_projects(user.id, limit=10)
        if project.status != ProjectStatus.CANCELLED.value
    ]
    rows = [
        [
            InlineKeyboardButton(
                text=f"🔄 #{project.id} · {project.name[:35]}",
                callback_data=f"studio:reopenproject:{project.id}",
            )
        ]
        for project in projects
    ]
    rows.append([InlineKeyboardButton(text="⬅️ الاستوديو", callback_data="menu:studio")])
    await _safe_bound_edit(
        callback.message,
        "🔄 اختر مشروعًا سابقًا. سنستخدم Timeline الأصلي وتعديلاته، وليس الفيديو النهائي المسطح:",
        InlineKeyboardMarkup(inline_keyboard=rows),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("studio:reopenproject:"))
async def reopen_previous_project(callback: CallbackQuery, state: FSMContext) -> None:
    project_id = int(callback.data.rsplit(":", 1)[1])
    owned = await _owned_project(callback, project_id)
    if owned is None or callback.message is None:
        return
    user, _ = owned
    try:
        await project_service.reopen_project(project_id, user_id=user.id)
    except ValueError as exc:
        await callback.answer(str(exc)[:180], show_alert=True)
        return
    await state.set_state(StudioState.collecting_media)
    await state.update_data(
        studio_project_id=project_id,
        studio_workflow=StudioWorkflow.REOPEN.value,
        studio_phase=StudioPhase.COLLECTING_MEDIA.value,
        recent_asset_id=None,
    )
    await _safe_bound_edit(
        callback.message,
        f"Project #{project_id}\n\n{_WORKFLOW_PROMPTS[StudioWorkflow.REOPEN]}",
        collecting_keyboard(project_id),
    )
    await callback.answer("تم فتح Timeline الأصلي")


@router.callback_query(F.data == "studio:projects")
async def my_projects(callback: CallbackQuery) -> None:
    user = await _callback_user(callback)
    if user is None or callback.message is None:
        return
    projects = await project_service.list_projects(user.id, limit=10)
    lines = ["📋 مشاريعي"]
    rows: list[list[InlineKeyboardButton]] = []
    for project in projects:
        count = len(await project_service.list_assets(project.id, user_id=user.id))
        lines.append(f"#{project.id} {project.status} — {project.name[:35]} — {count} assets")
        label = (
            f"♻️ إعادة مونتاج #{project.id}"
            if project.status == ProjectStatus.COMPLETED.value
            else f"فتح #{project.id}"
        )
        rows.append([InlineKeyboardButton(text=label, callback_data=f"studio:open:{project.id}")])
    if not projects:
        lines.append("\nلا توجد مشاريع بعد.")
    rows.append([InlineKeyboardButton(text="⬅️ الاستوديو", callback_data="menu:studio")])
    await _safe_bound_edit(
        callback.message,
        "\n".join(lines),
        InlineKeyboardMarkup(inline_keyboard=rows),
    )
    await callback.answer()


@router.callback_query(StudioState.collecting, F.data == "menu:home")
async def leave_studio(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    from app.bot.handlers import home

    await home(callback)
