from __future__ import annotations

import re

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import select

from app.bot.access import ensure_user, is_allowed
from app.config import get_settings
from app.db import SessionLocal, User
from app.i18n import tr
from app.security import redact_secrets
from app.services.openai_compatible import AIProviderError, get_openai_compatible_provider

settings = get_settings()
router = Router(name="ai-chat")
provider = get_openai_compatible_provider()
_MODEL_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")


class AIState(StatesGroup):
    model_name = State()
    chatting = State()


def enhanced_menu_keyboard(language: str = "ar") -> InlineKeyboardMarkup:
    if language == "en":
        labels = {
            "video": "🎬 Video",
            "audio": "🎵 MP3",
            "cut": "✂️ Cut",
            "history": "📋 My downloads",
            "bulk": "📥 Multiple URLs",
            "ai": "🤖 AI Chat",
            "lang": "🌐 Language",
            "help": "ℹ️ Help",
        }
    else:
        labels = {
            "video": "🎬 تحميل فيديو",
            "audio": "🎵 MP3",
            "cut": "✂️ قص",
            "history": "📋 تحميلاتي",
            "bulk": "📥 تحميل عدة روابط",
            "ai": "🤖 دردشة AI",
            "lang": "🌐 اللغة",
            "help": "ℹ️ مساعدة",
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
            [InlineKeyboardButton(text=labels["bulk"], callback_data="menu:bulk")],
            [InlineKeyboardButton(text=labels["ai"], callback_data="menu:ai")],
            [
                InlineKeyboardButton(text=labels["lang"], callback_data="menu:lang"),
                InlineKeyboardButton(text=labels["help"], callback_data="menu:help"),
            ],
        ]
    )


def _chat_controls(language: str = "ar") -> InlineKeyboardMarkup:
    if language == "en":
        new_text, models_text, home_text = "🧹 New chat", "🔄 Change model", "🏠 Home"
    else:
        new_text, models_text, home_text = "🧹 محادثة جديدة", "🔄 تغيير النموذج", "🏠 الرئيسية"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=new_text, callback_data="ai:new"),
                InlineKeyboardButton(text=models_text, callback_data="ai:models"),
            ],
            [InlineKeyboardButton(text=home_text, callback_data="menu:home")],
        ]
    )


def _models_keyboard(models: list[str], language: str = "ar") -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for index, model in enumerate(models[:20]):
        label = model if len(model) <= 48 else model[:45] + "…"
        rows.append([InlineKeyboardButton(text=label, callback_data=f"aimodel:{index}")])
    manual = "⌨️ Enter model ID" if language == "en" else "⌨️ إدخال Model ID يدويًا"
    home = "🏠 Home" if language == "en" else "🏠 الرئيسية"
    rows.append([InlineKeyboardButton(text=manual, callback_data="ai:manual-model")])
    rows.append([InlineKeyboardButton(text=home, callback_data="menu:home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _allowed_message(message: Message) -> bool:
    return bool(message.from_user and await is_allowed(message.from_user.id))


async def _callback_user(callback: CallbackQuery) -> User | None:
    if not callback.from_user or not await is_allowed(callback.from_user.id):
        await callback.answer("Private bot", show_alert=True)
        return None
    return await ensure_user(callback.from_user.id, callback.from_user.username)


async def _show_models(target: Message, state: FSMContext, language: str = "ar") -> None:
    if not settings.ai_enabled:
        text = (
            "🤖 AI is disabled. Configure OPENAI_BASE_URL and OPENAI_API_TOKEN in Railway Variables, then redeploy."
            if language == "en"
            else "🤖 خدمة AI غير مفعلة. أضف OPENAI_BASE_URL وOPENAI_API_TOKEN في Railway Variables ثم أعد النشر."
        )
        await target.answer(text, reply_markup=enhanced_menu_keyboard(language))
        return
    status = await target.answer(
        "🤖 Reading models from the configured provider…"
        if language == "en"
        else "🤖 جارٍ الاتصال بالمزود وقراءة النماذج المتاحة…"
    )
    try:
        models = await provider.list_models()
    except AIProviderError as exc:
        await state.set_state(AIState.model_name)
        await state.update_data(ai_models=[])
        safe = redact_secrets(str(exc), api_token=settings.openai_api_token)[:240]
        text = (
            f"The provider did not return a usable /models list ({safe}). Send the model ID exactly as your provider names it."
            if language == "en"
            else f"لم يُرجع المزود قائمة /models قابلة للاستخدام ({safe}). أرسل Model ID كما يسميه المزود بالضبط."
        )
        await status.edit_text(text, reply_markup=_chat_controls(language))
        return
    await state.update_data(ai_models=models)
    await status.edit_text(
        "Choose a model:" if language == "en" else "اختر النموذج الذي تريد استخدامه:",
        reply_markup=_models_keyboard(models, language),
    )


async def _start_chat(target: Message, state: FSMContext, model: str, language: str) -> None:
    data = await state.get_data()
    models = list(data.get("ai_models") or [])
    await state.set_state(AIState.chatting)
    await state.update_data(ai_model=model, ai_models=models, ai_history=[])
    text = (
        f"🤖 Model: {model}\nSend your message. Conversation context is kept for this bot session."
        if language == "en"
        else f"🤖 النموذج: {model}\nأرسل رسالتك الآن. سأحافظ على سياق المحادثة أثناء جلسة البوت الحالية."
    )
    await target.answer(text, reply_markup=_chat_controls(language))


@router.message(CommandStart())
async def enhanced_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    if not await _allowed_message(message) or not message.from_user:
        await message.answer("هذا المستخدم غير مسموح له حاليًا.")
        return
    user = await ensure_user(message.from_user.id, message.from_user.username)
    text = (
        f"مرحبًا بك في {settings.app_name}. اختر الخدمة المطلوبة."
        if user.language == "ar"
        else f"Welcome to {settings.app_name}. Choose a service."
    )
    await message.answer(text, reply_markup=enhanced_menu_keyboard(user.language))


@router.callback_query(F.data == "menu:home")
async def enhanced_home(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    user = await _callback_user(callback)
    if user is None:
        return
    text = "اختر الخدمة المطلوبة:" if user.language == "ar" else "Choose a service:"
    if callback.message:
        try:
            await callback.message.edit_text(text, reply_markup=enhanced_menu_keyboard(user.language))
        except Exception:
            await callback.message.answer(text, reply_markup=enhanced_menu_keyboard(user.language))
    await callback.answer()


@router.callback_query(F.data == "menu:help")
async def enhanced_help(callback: CallbackQuery) -> None:
    user = await _callback_user(callback)
    if user is None:
        return
    text = tr(user.language, "help")
    ai_note = (
        "\n\n🤖 AI Chat uses the OpenAI-compatible provider configured by the service owner."
        if user.language == "en"
        else "\n\n🤖 دردشة AI تستخدم مزود OpenAI-compatible الذي يضبطه مالك الخدمة."
    )
    if callback.message:
        await callback.message.edit_text(text + ai_note, reply_markup=enhanced_menu_keyboard(user.language))
    await callback.answer()


@router.callback_query(F.data.startswith("lang:"))
async def enhanced_language(callback: CallbackQuery, state: FSMContext) -> None:
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
    await state.clear()
    if callback.message:
        await callback.message.edit_text(
            tr(language, "welcome", name=settings.app_name),
            reply_markup=enhanced_menu_keyboard(language),
        )
    await callback.answer()


@router.message(Command("ai"))
async def ai_command(message: Message, state: FSMContext) -> None:
    if not await _allowed_message(message) or not message.from_user:
        await message.answer("هذا المستخدم غير مسموح له حاليًا.")
        return
    user = await ensure_user(message.from_user.id, message.from_user.username)
    await _show_models(message, state, user.language)


@router.callback_query(F.data == "menu:ai")
async def ai_menu(callback: CallbackQuery, state: FSMContext) -> None:
    user = await _callback_user(callback)
    if user is None:
        return
    await callback.answer()
    if callback.message:
        await _show_models(callback.message, state, user.language)


@router.callback_query(F.data == "ai:models")
async def ai_models(callback: CallbackQuery, state: FSMContext) -> None:
    user = await _callback_user(callback)
    if user is None:
        return
    await callback.answer()
    if callback.message:
        await _show_models(callback.message, state, user.language)


@router.callback_query(F.data == "ai:manual-model")
async def ai_manual_model(callback: CallbackQuery, state: FSMContext) -> None:
    user = await _callback_user(callback)
    if user is None:
        return
    await state.set_state(AIState.model_name)
    if callback.message:
        await callback.message.answer(
            "Send the exact model ID." if user.language == "en" else "أرسل Model ID بالاسم المطابق لدى المزود."
        )
    await callback.answer()


@router.message(AIState.model_name, F.text)
async def ai_model_name(message: Message, state: FSMContext) -> None:
    if not await _allowed_message(message) or not message.from_user or not message.text:
        return
    user = await ensure_user(message.from_user.id, message.from_user.username)
    model = message.text.strip()
    if not _MODEL_ID.fullmatch(model):
        await message.answer(
            "Invalid model ID. Use only letters, numbers, dot, dash, underscore, colon or slash."
            if user.language == "en"
            else "Model ID غير صالح. استخدم أحرفًا وأرقامًا و . - _ : / فقط."
        )
        return
    await _start_chat(message, state, model, user.language)


@router.callback_query(F.data == "ai:new")
async def ai_new_chat(callback: CallbackQuery, state: FSMContext) -> None:
    user = await _callback_user(callback)
    if user is None:
        return
    data = await state.get_data()
    model = str(data.get("ai_model") or "")
    models = list(data.get("ai_models") or [])
    await state.clear()
    if model and callback.message:
        await state.set_state(AIState.chatting)
        await state.update_data(ai_model=model, ai_models=models, ai_history=[])
        text = f"🧹 New chat with {model}." if user.language == "en" else f"🧹 بدأت محادثة جديدة مع {model}."
        await callback.message.answer(text, reply_markup=_chat_controls(user.language))
    elif callback.message:
        await _show_models(callback.message, state, user.language)
    await callback.answer()


@router.callback_query(F.data.startswith("aimodel:"))
async def ai_choose_model(callback: CallbackQuery, state: FSMContext) -> None:
    user = await _callback_user(callback)
    if user is None:
        return
    data = await state.get_data()
    models = list(data.get("ai_models") or [])
    try:
        index = int(callback.data.split(":", 1)[1])
        model = str(models[index])
    except (ValueError, IndexError):
        await callback.answer("Reopen the model list", show_alert=True)
        return
    if callback.message:
        await _start_chat(callback.message, state, model, user.language)
    await callback.answer()


@router.message(AIState.chatting, F.text)
async def ai_chat_message(message: Message, state: FSMContext) -> None:
    if not await _allowed_message(message) or not message.from_user or not message.text:
        return
    user = await ensure_user(message.from_user.id, message.from_user.username)
    data = await state.get_data()
    model = str(data.get("ai_model") or "")
    history = list(data.get("ai_history") or [])
    if not model:
        await state.clear()
        await _show_models(message, state, user.language)
        return

    history.append({"role": "user", "content": message.text[:8000]})
    thinking = await message.answer("🤖 Thinking…" if user.language == "en" else "🤖 يفكر…")
    try:
        reply = await provider.chat(model, history)
    except AIProviderError as exc:
        safe = redact_secrets(str(exc), api_token=settings.openai_api_token)[:300]
        text = f"AI request failed: {safe}" if user.language == "en" else f"تعذر إكمال طلب AI: {safe}"
        await thinking.edit_text(text, reply_markup=_chat_controls(user.language))
        return

    history.append({"role": "assistant", "content": reply.text})
    history = history[-settings.ai_max_history_messages :]
    await state.update_data(ai_history=history, ai_model=reply.model or model)
    try:
        await thinking.delete()
    except Exception:
        pass
    chunks = [reply.text[index : index + 3500] for index in range(0, len(reply.text), 3500)] or ["—"]
    for index, chunk in enumerate(chunks):
        await message.answer(
            chunk,
            reply_markup=_chat_controls(user.language) if index == len(chunks) - 1 else None,
        )
