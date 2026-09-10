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
from app.bot.uploads import download_upload, upload_candidate
from app.config import get_settings
from app.db import MediaProject, ProjectStatus, RenderStatus, User
from app.render_queue import cancel_render, enqueue_render
from app.services.assets import AssetService, asset_service
from app.services.composer import composer_service
from app.services.media import fit_media_for_upload, probe_media_file
from app.services.projects import ProjectAssetItem, ProjectService, project_service
from app.services.render_service import RenderService, render_service
from app.services.urls import parse_bulk_urls
from app.utils import seconds_to_hms

settings = get_settings()
router = Router(name="media-studio")
logger = logging.getLogger("moataz.studio.telegram")
_delivery_tasks: set[asyncio.Task] = set()


class StudioState(StatesGroup):
    collecting = State()


def studio_home_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🎬 مشروع جديد", callback_data="studio:new")],
            [InlineKeyboardButton(text="📋 مشاريعي", callback_data="studio:projects")],
            [InlineKeyboardButton(text="🏠 الرئيسية", callback_data="menu:home")],
        ]
    )


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
            [InlineKeyboardButton(text="🗑 إلغاء المشروع", callback_data=f"studio:cancelproject:{project_id}")],
            [InlineKeyboardButton(text="🏠 الرئيسية", callback_data="menu:home")],
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


async def _show_assets(message: Message, project_id: int, user_id: int) -> None:
    items = await project_service.list_assets(project_id, user_id=user_id)
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
    if not items:
        lines.append("\nلا توجد مواد بعد.")
    rows.append([InlineKeyboardButton(text="⬅️ المشروع", callback_data=f"studio:open:{project_id}")])
    await message.edit_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


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
    await message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


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


async def watch_render_and_deliver(
    bot: Bot,
    *,
    chat_id: int,
    message_id: int,
    render_job_id: int,
    user_id: int,
    service: RenderService = render_service,
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
            return
        if job.status == RenderStatus.UPLOADING.value:
            await asyncio.sleep(max(1.2, settings.progress_update_seconds))
            continue
        if job.status != RenderStatus.COMPLETED.value or not job.output_path:
            return

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
                caption=f"✅ Project #{job.project_id} • Render #{job.id}",
                supports_streaming=True,
            )
            await service.finish_delivery(job.id)
            await _safe_edit(bot, chat_id, message_id, f"✅ اكتمل Project #{job.project_id} وتم إرسال الفيديو.")
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


@router.callback_query(F.data == "menu:studio")
async def studio_home(callback: CallbackQuery, state: FSMContext) -> None:
    user = await _callback_user(callback)
    if user is None:
        return
    await state.clear()
    if callback.message:
        await callback.message.edit_text("🎞 استوديو المونتاج", reply_markup=studio_home_keyboard())
    await callback.answer()


@router.callback_query(F.data == "studio:new")
async def new_project(callback: CallbackQuery, state: FSMContext) -> None:
    user = await _callback_user(callback)
    if user is None or callback.message is None:
        return
    project = await project_service.create_project(user_id=user.id, chat_id=callback.message.chat.id)
    await state.set_state(StudioState.collecting)
    await state.update_data(studio_project_id=project.id)
    await callback.message.edit_text(_project_text(project.id), reply_markup=project_keyboard(project.id))
    await callback.answer()


@router.callback_query(F.data.startswith("studio:open:"))
async def open_project(callback: CallbackQuery, state: FSMContext) -> None:
    project_id = int(callback.data.rsplit(":", 1)[1])
    owned = await _owned_project(callback, project_id)
    if owned is None or callback.message is None:
        return
    user, project = owned
    if project.status != ProjectStatus.CANCELLED.value:
        await state.set_state(StudioState.collecting)
        await state.update_data(studio_project_id=project_id)
    items = await project_service.list_assets(project_id, user_id=user.id)
    await callback.message.edit_text(
        f"🎬 Project #{project_id} • {project.status}\nالمواد: {len(items)}\nAspect: {project.aspect_ratio}",
        reply_markup=project_keyboard(project_id),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("studio:add:"))
async def add_more(callback: CallbackQuery, state: FSMContext) -> None:
    project_id = int(callback.data.rsplit(":", 1)[1])
    if await _owned_project(callback, project_id) is None or callback.message is None:
        return
    await state.set_state(StudioState.collecting)
    await state.update_data(studio_project_id=project_id)
    await callback.message.edit_text(_project_text(project_id), reply_markup=project_keyboard(project_id))
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
        await projects.add_asset(project_id, asset.id, user_id=user.id)
        await message.answer(
            f"✅ أضيفت {_asset_icon(asset.asset_type)} {candidate.file_name[:60]}",
            reply_markup=project_keyboard(project_id),
        )
    except Exception as exc:
        if asset is not None:
            with suppress(Exception):
                await assets.delete_unattached_asset(asset.id, user_id=user.id)
        await message.answer(f"تعذر قبول الملف: {str(exc)[:300]}")
    finally:
        if temporary is not None:
            shutil.rmtree(temporary.parent, ignore_errors=True)


@router.message(
    StudioState.collecting,
    F.video | F.photo | F.audio | F.voice | F.document,
)
async def receive_upload(message: Message, state: FSMContext) -> None:
    await ingest_upload_message(message, state)


@router.message(StudioState.collecting, F.text)
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
            await project_service.add_asset(project_id, asset.id, user_id=user.id)
            added += 1
        except Exception as exc:
            if asset is not None:
                with suppress(Exception):
                    await asset_service.delete_unattached_asset(asset.id, user_id=user.id)
            failures.append(str(exc)[:100])
    text = f"✅ تمت إضافة {added} رابط إلى Project #{project_id}."
    if failures:
        text += f"\nتعذر {len(failures)}: {failures[0]}"
    await progress.edit_text(text, reply_markup=project_keyboard(project_id))


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
    await callback.message.edit_text("🏷 اختر دور المادة:", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
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
    await callback.message.edit_text("🎬 اختر طريقة المونتاج:", reply_markup=markup)
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
    await callback.message.edit_text(
        _progress_text(project_id, 0.0, time.monotonic()),
        reply_markup=render_cancel_keyboard(job.id),
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
        await callback.message.edit_text("🗑 تم إلغاء المشروع.", reply_markup=studio_home_keyboard())
    await callback.answer()


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
        rows.append([InlineKeyboardButton(text=f"فتح #{project.id}", callback_data=f"studio:open:{project.id}")])
    if not projects:
        lines.append("\nلا توجد مشاريع بعد.")
    rows.append([InlineKeyboardButton(text="⬅️ الاستوديو", callback_data="menu:studio")])
    await callback.message.edit_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await callback.answer()


@router.callback_query(StudioState.collecting, F.data == "menu:home")
async def leave_studio(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    from app.bot.handlers import home

    await home(callback)
