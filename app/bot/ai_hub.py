from __future__ import annotations

import io
import re
from pathlib import Path

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.bot.access import ensure_user, is_allowed
from app.bot.ai import _models_keyboard, _show_models, enhanced_menu_keyboard
from app.config import get_settings
from app.services.ai_registry import CatalogModel, get_ai_provider_registry
from app.services.ai_workspace import (
    PROJECT_SYSTEM_PROMPT,
    WorkspaceError,
    build_project_zip,
    cleanup_artifact,
    read_source_url,
    summarize_file_bytes,
)
from app.services.model_capabilities import capability_icon
from app.services.openai_compatible import AIProviderError

settings = get_settings()
registry = get_ai_provider_registry()
router = Router(name="ai-hub")
_URL_RE = re.compile(r"https?://[^\s<>]+", re.IGNORECASE)
_PROJECT_CONTEXT_LIMIT = 120_000


class ProjectState(StatesGroup):
    brief = State()


def _hub_keyboard(language: str) -> InlineKeyboardMarkup:
    if language == "en":
        labels = {
            "all": "💬 All chat models",
            "free": "🆓 Free",
            "vision": "👁️ Vision / image input",
            "image": "🖼️ Image generation",
            "video": "🎬 Video",
            "audio": "🔊 Audio",
            "code": "🧑‍💻 Coding",
            "tools": "🧰 Tools / agents",
            "project": "📦 Build / modify project",
            "providers": "🔌 Provider status",
            "home": "🏠 Home",
        }
    else:
        labels = {
            "all": "💬 كل نماذج الدردشة",
            "free": "🆓 المجانية",
            "vision": "👁️ رؤية / إدخال صور",
            "image": "🖼️ توليد الصور",
            "video": "🎬 الفيديو",
            "audio": "🔊 الصوت",
            "code": "🧑‍💻 البرمجة",
            "tools": "🧰 الأدوات والوكلاء",
            "project": "📦 بناء / تعديل مشروع",
            "providers": "🔌 حالة المزودين",
            "home": "🏠 الرئيسية",
        }
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=labels["all"], callback_data="aihub:all")],
            [
                InlineKeyboardButton(text=labels["free"], callback_data="aihub:cap:free"),
                InlineKeyboardButton(text=labels["vision"], callback_data="aihub:cap:vision"),
            ],
            [
                InlineKeyboardButton(text=labels["image"], callback_data="aihub:cap:image"),
                InlineKeyboardButton(text=labels["video"], callback_data="aihub:cap:video"),
            ],
            [
                InlineKeyboardButton(text=labels["audio"], callback_data="aihub:cap:audio"),
                InlineKeyboardButton(text=labels["code"], callback_data="aihub:cap:code"),
            ],
            [InlineKeyboardButton(text=labels["tools"], callback_data="aihub:cap:tools")],
            [InlineKeyboardButton(text=labels["project"], callback_data="aihub:project")],
            [InlineKeyboardButton(text=labels["providers"], callback_data="aihub:providers")],
            [InlineKeyboardButton(text=labels["home"], callback_data="menu:home")],
        ]
    )


async def _callback_user(callback: CallbackQuery):
    if not callback.from_user or not await is_allowed(callback.from_user.id):
        await callback.answer("Private bot", show_alert=True)
        return None
    return await ensure_user(callback.from_user.id, callback.from_user.username)


def _capability_title(capability: str, language: str) -> str:
    names_en = {
        "free": "Free models",
        "vision": "Vision / image-input models",
        "image": "Image-generation models",
        "video": "Video models",
        "audio": "Audio models",
        "code": "Coding models",
        "tools": "Tool/agent models",
    }
    names_ar = {
        "free": "النماذج المجانية",
        "vision": "نماذج الرؤية وإدخال الصور",
        "image": "نماذج توليد الصور",
        "video": "نماذج الفيديو",
        "audio": "نماذج الصوت",
        "code": "نماذج البرمجة",
        "tools": "نماذج الأدوات والوكلاء",
    }
    return (names_en if language == "en" else names_ar).get(capability, capability)


async def _show_hub(target: Message, language: str) -> None:
    if language == "en":
        text = (
            "🤖 AI Center\nChoose a capability. Models are grouped by metadata reported by each provider; "
            "free models keep priority inside every group."
        )
    else:
        text = (
            "🤖 مركز الذكاء الاصطناعي\nاختر نوع القدرة المطلوبة. يتم تصنيف النماذج حسب البيانات التي يعلنها كل مزود، "
            "مع إبقاء النماذج المجانية في الأولوية داخل كل قسم."
        )
    await target.answer(text, reply_markup=_hub_keyboard(language))


@router.callback_query(F.data == "menu:ai")
async def ai_hub_menu(callback: CallbackQuery, state: FSMContext) -> None:
    user = await _callback_user(callback)
    if user is None:
        return
    await state.clear()
    if callback.message:
        await _show_hub(callback.message, user.language)
    await callback.answer()


@router.callback_query(F.data == "aihub:all")
async def ai_hub_all(callback: CallbackQuery, state: FSMContext) -> None:
    user = await _callback_user(callback)
    if user is None:
        return
    if callback.message:
        await _show_models(callback.message, state, user.language)
    await callback.answer()


@router.callback_query(F.data.startswith("aihub:cap:"))
async def ai_hub_capability(callback: CallbackQuery, state: FSMContext) -> None:
    user = await _callback_user(callback)
    if user is None:
        return
    capability = callback.data.rsplit(":", 1)[-1]
    if capability not in {"free", "vision", "image", "video", "audio", "code", "tools"}:
        await callback.answer()
        return
    if callback.message:
        status = await callback.message.answer(
            "🔎 Reading provider catalogs…" if user.language == "en" else "🔎 جارٍ قراءة كتالوجات المزودين…"
        )
        models, errors = await registry.models_for(capability)
        serialized = [model.state_dict() for model in models]
        await state.update_data(
            ai_models=serialized,
            ai_providers=[provider.public_dict() for provider in registry.providers],
            ai_provider_errors=list(errors),
        )
        title = _capability_title(capability, user.language)
        if not serialized:
            text = (
                f"{capability_icon(capability)} {title}: no models were advertised with this capability.\n"
                "Some providers do not expose complete capability metadata; use All chat models or manual Model ID when needed."
                if user.language == "en"
                else f"{capability_icon(capability)} {title}: لم يعلن أي مزود حاليًا عن نماذج بهذه القدرة.\n"
                "بعض المزودين لا يعيدون بيانات قدرات كاملة؛ استخدم كل نماذج الدردشة أو أدخل Model ID يدويًا عند الحاجة."
            )
            await status.edit_text(text, reply_markup=_hub_keyboard(user.language))
        else:
            free_count = sum(model.get("is_free") is True for model in serialized)
            text = (
                f"{capability_icon(capability)} {title}: {len(serialized)} • Free: {free_count}"
                if user.language == "en"
                else f"{capability_icon(capability)} {title}: {len(serialized)} • المجانية: {free_count}"
            )
            await status.edit_text(text, reply_markup=_models_keyboard(serialized, user.language, 0))
    await callback.answer()


@router.callback_query(F.data == "aihub:providers")
async def ai_hub_providers(callback: CallbackQuery) -> None:
    user = await _callback_user(callback)
    if user is None:
        return
    if callback.message:
        status = await callback.message.answer(
            "🔌 Checking providers…" if user.language == "en" else "🔌 جارٍ فحص المزودين…"
        )
        checks = await registry.probe()
        lines: list[str] = []
        for check in checks:
            name = str(check.get("provider_name") or check.get("provider_id") or "AI")
            if check.get("ok") is True:
                count = int(check.get("model_count") or 0)
                capabilities = ", ".join(str(item) for item in check.get("capabilities") or []) or "text"
                lines.append(f"✅ {name}: {count} • {capabilities}")
            else:
                error = str(check.get("error") or "unavailable")
                lines.append(f"❌ {name}: {error[:120]}")
        text = "\n".join(lines) or ("No providers configured." if user.language == "en" else "لا يوجد مزودون مضافون.")
        await status.edit_text(text, reply_markup=_hub_keyboard(user.language))
    await callback.answer()


def _project_help(language: str) -> str:
    if language == "en":
        return (
            "📦 Project workspace\n"
            "Send a project brief directly, or first send one source then your requested changes:\n"
            "• a public GitHub repository URL\n"
            "• a web/documentation URL\n"
            "• a source file or ZIP archive\n\n"
            "The bot reads safe text files, asks a coding-capable model to build/modify the project, and returns a ready ZIP."
        )
    return (
        "📦 مساحة المشاريع\n"
        "أرسل وصف المشروع مباشرة، أو أرسل أولًا مصدرًا ثم أرسل التعديلات المطلوبة:\n"
        "• رابط مستودع GitHub عام\n"
        "• رابط صفحة أو توثيق\n"
        "• ملف مصدر أو ZIP\n\n"
        "سيقرأ البوت الملفات النصية الآمنة، ويستخدم نموذجًا مناسبًا للبرمجة لبناء/تعديل المشروع، ثم يعيد ZIP جاهزًا."
    )


@router.callback_query(F.data == "aihub:project")
async def ai_hub_project(callback: CallbackQuery, state: FSMContext) -> None:
    user = await _callback_user(callback)
    if user is None:
        return
    await state.clear()
    await state.set_state(ProjectState.brief)
    await state.update_data(project_context="")
    if callback.message:
        await callback.message.answer(_project_help(user.language))
    await callback.answer()


@router.message(Command("project"))
async def ai_project_command(message: Message, state: FSMContext) -> None:
    if not message.from_user or not await is_allowed(message.from_user.id):
        return
    user = await ensure_user(message.from_user.id, message.from_user.username)
    await state.clear()
    await state.set_state(ProjectState.brief)
    await state.update_data(project_context="")
    await message.answer(_project_help(user.language))


async def _store_context(message: Message, state: FSMContext, context: str, language: str) -> None:
    compact = context[:_PROJECT_CONTEXT_LIMIT]
    await state.update_data(project_context=compact)
    await message.answer(
        f"✅ Source loaded ({len(compact):,} characters). Now send what you want to build or change."
        if language == "en"
        else f"✅ تم تحميل المصدر ({len(compact):,} حرفًا). أرسل الآن ما تريد بناءه أو تعديله."
    )


@router.message(ProjectState.brief, F.document)
async def ai_project_document(message: Message, state: FSMContext) -> None:
    if not message.from_user or not message.document or not await is_allowed(message.from_user.id):
        return
    user = await ensure_user(message.from_user.id, message.from_user.username)
    if message.document.file_size and message.document.file_size > 4 * 1024 * 1024:
        await message.answer("File is too large for source inspection." if user.language == "en" else "الملف كبير جدًا لفحص المصدر.")
        return
    buffer = io.BytesIO()
    try:
        if message.bot is None:
            raise WorkspaceError("Telegram client is unavailable")
        await message.bot.download(message.document, destination=buffer)
        context = summarize_file_bytes(buffer.getvalue(), message.document.file_name or "source")
    except (WorkspaceError, OSError, ValueError) as exc:
        await message.answer(
            f"Could not read source: {exc}" if user.language == "en" else f"تعذر قراءة المصدر: {exc}"
        )
        return
    await _store_context(message, state, context, user.language)


def _pick_project_model(models: list[CatalogModel]) -> CatalogModel | None:
    for capability in ("code", "tools", "text"):
        for model in models:
            if capability in model.capabilities:
                return model
    return models[0] if models else None


async def _generate_project(message: Message, state: FSMContext, brief: str, language: str) -> None:
    data = await state.get_data()
    context = str(data.get("project_context") or "")
    status = await message.answer(
        "🧑‍💻 Building project files…" if language == "en" else "🧑‍💻 جارٍ بناء ملفات المشروع…"
    )
    models, errors = await registry.list_models()
    model = _pick_project_model(models)
    if model is None:
        unavailable = ", ".join(errors) or "all providers"
        await status.edit_text(
            f"No usable AI model is available ({unavailable})."
            if language == "en"
            else f"لا يوجد نموذج AI متاح حاليًا ({unavailable})."
        )
        return

    source_section = f"\n\nSOURCE CONTEXT:\n{context}" if context else ""
    user_prompt = f"PROJECT REQUEST:\n{brief.strip()}{source_section}"
    try:
        reply = await registry.chat(
            model.provider_id,
            model.model_id,
            [
                {"role": "system", "content": PROJECT_SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
        )
        artifact = build_project_zip(reply.text, Path(settings.download_dir))
    except (AIProviderError, WorkspaceError, OSError, ValueError) as exc:
        await status.edit_text(
            f"Project generation failed: {str(exc)[:300]}"
            if language == "en"
            else f"تعذر إنشاء المشروع: {str(exc)[:300]}"
        )
        return

    try:
        caption = (
            f"✅ {artifact.name} • {artifact.file_count} files • generated with {model.provider_name} / {model.model_id}"
            if language == "en"
            else f"✅ {artifact.name} • {artifact.file_count} ملف • تم إنشاؤه عبر {model.provider_name} / {model.model_id}"
        )
        await message.answer_document(FSInputFile(artifact.zip_path, filename=f"{artifact.name}.zip"), caption=caption[:900])
        await status.edit_text(
            "✅ Project ZIP is ready." if language == "en" else "✅ ملف ZIP للمشروع جاهز."
        )
        await state.clear()
    finally:
        cleanup_artifact(artifact)


@router.message(ProjectState.brief, F.text)
async def ai_project_text(message: Message, state: FSMContext) -> None:
    if not message.from_user or not message.text or not await is_allowed(message.from_user.id):
        return
    user = await ensure_user(message.from_user.id, message.from_user.username)
    text = message.text.strip()
    data = await state.get_data()
    context = str(data.get("project_context") or "")
    match = _URL_RE.search(text)

    if match and not context:
        url = match.group(0).rstrip(".,;)")
        remaining = (text[: match.start()] + text[match.end() :]).strip()
        loading = await message.answer("🔗 Reading source…" if user.language == "en" else "🔗 جارٍ قراءة المصدر…")
        try:
            source = await read_source_url(url)
        except WorkspaceError as exc:
            await loading.edit_text(
                f"Could not read URL: {exc}" if user.language == "en" else f"تعذر قراءة الرابط: {exc}"
            )
            return
        await state.update_data(project_context=source[:_PROJECT_CONTEXT_LIMIT])
        if not remaining:
            await loading.edit_text(
                "✅ Source loaded. Now send what you want to build or change."
                if user.language == "en"
                else "✅ تم تحميل المصدر. أرسل الآن ما تريد بناءه أو تعديله."
            )
            return
        await loading.delete()
        text = remaining

    if len(text) < 3:
        await message.answer("Describe the project or requested changes." if user.language == "en" else "اكتب وصف المشروع أو التعديلات المطلوبة.")
        return
    await _generate_project(message, state, text, user.language)


@router.message(Command("aihub"))
async def ai_hub_command(message: Message, state: FSMContext) -> None:
    if not message.from_user or not await is_allowed(message.from_user.id):
        return
    user = await ensure_user(message.from_user.id, message.from_user.username)
    await state.clear()
    await _show_hub(message, user.language)


@router.message(Command("menu"))
async def ai_menu_command(message: Message, state: FSMContext) -> None:
    if not message.from_user or not await is_allowed(message.from_user.id):
        return
    user = await ensure_user(message.from_user.id, message.from_user.username)
    await state.clear()
    await message.answer(
        "Choose a service:" if user.language == "en" else "اختر الخدمة المطلوبة:",
        reply_markup=enhanced_menu_keyboard(user.language),
    )
