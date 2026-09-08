from __future__ import annotations

import math
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
from app.services.ai_registry import get_ai_provider_registry
from app.services.openai_compatible import AIProviderError

settings = get_settings()
router = Router(name="ai-chat")
registry = get_ai_provider_registry()
_MODEL_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
_MODEL_PAGE_SIZE = 12


class AIState(StatesGroup):
    provider_name = State()
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


def _model_label(model: dict[str, object]) -> str:
    provider = str(model.get("provider_name") or "AI")
    model_id = str(model.get("model_id") or "unknown")
    prefix = "🆓 " if model.get("is_free") is True else ""
    label = f"{prefix}{provider} • {model_id}"
    return label if len(label) <= 58 else label[:55] + "…"


def _models_keyboard(
    models: list[dict[str, object]],
    language: str = "ar",
    page: int = 0,
) -> InlineKeyboardMarkup:
    total_pages = max(1, math.ceil(len(models) / _MODEL_PAGE_SIZE))
    page = max(0, min(page, total_pages - 1))
    start = page * _MODEL_PAGE_SIZE
    stop = min(len(models), start + _MODEL_PAGE_SIZE)
    rows: list[list[InlineKeyboardButton]] = []
    for index in range(start, stop):
        rows.append(
            [
                InlineKeyboardButton(
                    text=_model_label(models[index]),
                    callback_data=f"aimodel:{index}",
                )
            ]
        )

    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"ai:page:{page - 1}"))
    if total_pages > 1:
        nav.append(InlineKeyboardButton(text=f"{page + 1}/{total_pages}", callback_data="ai:noop"))
    if page + 1 < total_pages:
        nav.append(InlineKeyboardButton(text="➡️", callback_data=f"ai:page:{page + 1}"))
    if nav:
        rows.append(nav)

    manual = "⌨️ Enter model ID" if language == "en" else "⌨️ إدخال Model ID يدويًا"
    home = "🏠 Home" if language == "en" else "🏠 الرئيسية"
    rows.append([InlineKeyboardButton(text=manual, callback_data="ai:manual-model")])
    rows.append([InlineKeyboardButton(text=home, callback_data="menu:home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _providers_keyboard(providers: list[dict[str, object]], language: str = "ar") -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for index, provider in enumerate(providers[:40]):
        name = str(provider.get("name") or provider.get("provider_id") or "AI")
        rows.append([InlineKeyboardButton(text=f"🔌 {name}"[:58], callback_data=f"aiprovider:{index}")])
    home = "🏠 Home" if language == "en" else "🏠 الرئيسية"
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
    providers = [provider.public_dict() for provider in registry.providers]
    if not registry.enabled:
        text = (
            "🤖 AI is disabled. Configure at least one OpenAI-compatible provider in Railway Variables."
            if language == "en"
            else "🤖 خدمة AI غير مفعلة. أضف مزود OpenAI-compatible واحدًا على الأقل في Railway Variables."
        )
        await target.answer(text, reply_markup=enhanced_menu_keyboard(language))
        return

    status = await target.answer(
        "🤖 Reading models from all configured providers…"
        if language == "en"
        else "🤖 جارٍ قراءة النماذج من جميع المزودين المضافين…"
    )
    models, errors = await registry.list_models()
    serialized = [model.state_dict() for model in models]
    await state.update_data(
        ai_models=serialized,
        ai_providers=providers,
        ai_provider_errors=list(errors),
    )

    if not serialized:
        await state.set_state(AIState.provider_name)
        text = (
            "No provider returned a usable /models list. Choose a provider and enter a model ID manually."
            if language == "en"
            else "لم يُرجع أي مزود قائمة /models قابلة للاستخدام. اختر المزود ثم أدخل Model ID يدويًا."
        )
        await status.edit_text(text, reply_markup=_providers_keyboard(providers, language))
        return

    free_count = sum(model.get("is_free") is True for model in serialized)
    provider_count = len({str(model.get("provider_id") or "") for model in serialized})
    unavailable = len(errors)
    if language == "en":
        text = f"🤖 Models: {len(serialized)} • 🆓 Free: {free_count} • Providers: {provider_count}"
        if unavailable:
            text += f" • Unavailable: {unavailable}"
        text += "\nFree models are shown first."
    else:
        text = f"🤖 النماذج: {len(serialized)} • 🆓 المجانية: {free_count} • المزودون: {provider_count}"
        if unavailable:
            text += f" • متعذر مؤقتًا: {unavailable}"
        text += "\nتظهر النماذج المجانية أولًا."
    await status.edit_text(text, reply_markup=_models_keyboard(serialized, language, 0))


async def _start_chat(
    target: Message,
    state: FSMContext,
    provider_id: str,
    provider_name: str,
    model: str,
    language: str,
) -> None:
    data = await state.get_data()
    models = list(data.get("ai_models") or [])
    providers = list(data.get("ai_providers") or [])
    await state.set_state(AIState.chatting)
    await state.update_data(
        ai_provider_id=provider_id,
        ai_provider_name=provider_name,
        ai_model=model,
        ai_models=models,
        ai_providers=providers,
        ai_history=[],
    )
    text = (
        f"🤖 Provider: {provider_name}\nModel: {model}\nSend your message. Conversation context is kept for this bot session."
        if language == "en"
        else f"🤖 المزود: {provider_name}\nالنموذج: {model}\nأرسل رسالتك الآن. سأحافظ على سياق المحادثة أثناء جلسة البوت الحالية."
    )
    await target.answer(text, reply_markup=_chat_controls(language))


async def _ask_manual_provider(target: Message, state: FSMContext, language: str) -> None:
    providers = [provider.public_dict() for provider in registry.providers]
    await state.update_data(ai_providers=providers)
    if len(providers) == 1:
        provider = providers[0]
        await state.set_state(AIState.model_name)
        await state.update_data(
            ai_manual_provider_id=str(provider["provider_id"]),
            ai_manual_provider_name=str(provider["name"]),
        )
        await target.answer(
            "Send the exact model ID." if language == "en" else "أرسل Model ID بالاسم المطابق لدى المزود."
        )
        return
    await state.set_state(AIState.provider_name)
    await target.answer(
        "Choose the provider for the manual model ID:" if language == "en" else "اختر المزود الذي تريد إدخال Model ID له:",
        reply_markup=_providers_keyboard(providers, language),
    )


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
        "\n\n🤖 AI Chat aggregates models from all OpenAI-compatible providers configured by the service owner."
        if user.language == "en"
        else "\n\n🤖 دردشة AI تجمع النماذج من جميع مزودي OpenAI-compatible الذين يضبطهم مالك الخدمة."
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


@router.callback_query(F.data.startswith("ai:page:"))
async def ai_models_page(callback: CallbackQuery, state: FSMContext) -> None:
    user = await _callback_user(callback)
    if user is None:
        return
    data = await state.get_data()
    models = list(data.get("ai_models") or [])
    try:
        page = int(callback.data.rsplit(":", 1)[1])
    except ValueError:
        await callback.answer()
        return
    free_count = sum(isinstance(model, dict) and model.get("is_free") is True for model in models)
    if callback.message:
        text = (
            f"🤖 Models: {len(models)} • 🆓 Free: {free_count}\nFree models are shown first."
            if user.language == "en"
            else f"🤖 النماذج: {len(models)} • 🆓 المجانية: {free_count}\nتظهر النماذج المجانية أولًا."
        )
        await callback.message.edit_text(text, reply_markup=_models_keyboard(models, user.language, page))
    await callback.answer()


@router.callback_query(F.data == "ai:noop")
async def ai_noop(callback: CallbackQuery) -> None:
    await callback.answer()


@router.callback_query(F.data == "ai:manual-model")
async def ai_manual_model(callback: CallbackQuery, state: FSMContext) -> None:
    user = await _callback_user(callback)
    if user is None:
        return
    if callback.message:
        await _ask_manual_provider(callback.message, state, user.language)
    await callback.answer()


@router.callback_query(F.data.startswith("aiprovider:"))
async def ai_choose_manual_provider(callback: CallbackQuery, state: FSMContext) -> None:
    user = await _callback_user(callback)
    if user is None:
        return
    data = await state.get_data()
    providers = list(data.get("ai_providers") or [])
    try:
        index = int(callback.data.split(":", 1)[1])
        provider = providers[index]
        if not isinstance(provider, dict):
            raise IndexError
        provider_id = str(provider["provider_id"])
        provider_name = str(provider["name"])
    except (ValueError, IndexError, KeyError):
        await callback.answer("Reopen the provider list", show_alert=True)
        return
    await state.set_state(AIState.model_name)
    await state.update_data(
        ai_manual_provider_id=provider_id,
        ai_manual_provider_name=provider_name,
    )
    if callback.message:
        await callback.message.answer(
            f"Provider: {provider_name}\nSend the exact model ID."
            if user.language == "en"
            else f"المزود: {provider_name}\nأرسل Model ID بالاسم المطابق لدى المزود."
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
    data = await state.get_data()
    provider_id = str(data.get("ai_manual_provider_id") or "")
    provider_name = str(data.get("ai_manual_provider_name") or "")
    if not provider_id:
        await _ask_manual_provider(message, state, user.language)
        return
    await _start_chat(message, state, provider_id, provider_name or provider_id, model, user.language)


@router.callback_query(F.data == "ai:new")
async def ai_new_chat(callback: CallbackQuery, state: FSMContext) -> None:
    user = await _callback_user(callback)
    if user is None:
        return
    data = await state.get_data()
    provider_id = str(data.get("ai_provider_id") or "")
    provider_name = str(data.get("ai_provider_name") or provider_id)
    model = str(data.get("ai_model") or "")
    models = list(data.get("ai_models") or [])
    providers = list(data.get("ai_providers") or [])
    await state.clear()
    if provider_id and model and callback.message:
        await state.set_state(AIState.chatting)
        await state.update_data(
            ai_provider_id=provider_id,
            ai_provider_name=provider_name,
            ai_model=model,
            ai_models=models,
            ai_providers=providers,
            ai_history=[],
        )
        text = (
            f"🧹 New chat with {provider_name} • {model}."
            if user.language == "en"
            else f"🧹 بدأت محادثة جديدة مع {provider_name} • {model}."
        )
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
        selected = models[index]
        if not isinstance(selected, dict):
            raise IndexError
        provider_id = str(selected["provider_id"])
        provider_name = str(selected["provider_name"])
        model = str(selected["model_id"])
    except (ValueError, IndexError, KeyError):
        await callback.answer("Reopen the model list", show_alert=True)
        return
    if callback.message:
        await _start_chat(callback.message, state, provider_id, provider_name, model, user.language)
    await callback.answer()


@router.message(AIState.chatting, F.text)
async def ai_chat_message(message: Message, state: FSMContext) -> None:
    if not await _allowed_message(message) or not message.from_user or not message.text:
        return
    user = await ensure_user(message.from_user.id, message.from_user.username)
    data = await state.get_data()
    provider_id = str(data.get("ai_provider_id") or "")
    provider_name = str(data.get("ai_provider_name") or provider_id)
    model = str(data.get("ai_model") or "")
    history = list(data.get("ai_history") or [])
    if not provider_id or not model:
        await state.clear()
        await _show_models(message, state, user.language)
        return

    history.append({"role": "user", "content": message.text[:8000]})
    thinking = await message.answer("🤖 Thinking…" if user.language == "en" else "🤖 يفكر…")
    try:
        reply = await registry.chat(provider_id, model, history)
    except AIProviderError as exc:
        safe = str(exc)[:300]
        text = f"AI request failed: {safe}" if user.language == "en" else f"تعذر إكمال طلب AI: {safe}"
        await thinking.edit_text(text, reply_markup=_chat_controls(user.language))
        return

    history.append({"role": "assistant", "content": reply.text})
    history = history[-settings.ai_max_history_messages :]
    await state.update_data(
        ai_history=history,
        ai_provider_id=provider_id,
        ai_provider_name=provider_name,
        ai_model=reply.model or model,
    )
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
