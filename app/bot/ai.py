from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.bot.access import ensure_user, is_allowed
from app.config import get_settings
from app.services.openai_compatible import AIProviderError, get_openai_compatible_provider

settings = get_settings()
router = Router(name="ai-chat")
provider = get_openai_compatible_provider()


class AIState(StatesGroup):
    chatting = State()


def enhanced_menu_keyboard() -> InlineKeyboardMarkup:
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
            [InlineKeyboardButton(text="🤖 دردشة AI", callback_data="menu:ai")],
            [
                InlineKeyboardButton(text="🌐 اللغة", callback_data="menu:lang"),
                InlineKeyboardButton(text="ℹ️ مساعدة", callback_data="menu:help"),
            ],
        ]
    )


def _chat_controls() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🧹 محادثة جديدة", callback_data="ai:new"),
                InlineKeyboardButton(text="🔄 تغيير النموذج", callback_data="ai:models"),
            ],
            [InlineKeyboardButton(text="🏠 الرئيسية", callback_data="menu:home")],
        ]
    )


def _models_keyboard(models: list[str]) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for index, model in enumerate(models[:20]):
        label = model if len(model) <= 48 else model[:45] + "…"
        rows.append([InlineKeyboardButton(text=label, callback_data=f"aimodel:{index}")])
    rows.append([InlineKeyboardButton(text="🏠 الرئيسية", callback_data="menu:home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _allowed_message(message: Message) -> bool:
    return bool(message.from_user and await is_allowed(message.from_user.id))


async def _show_models(target: Message, state: FSMContext) -> None:
    if not settings.ai_enabled:
        await target.answer(
            "🤖 خدمة AI غير مفعلة بعد. أضف OPENAI_BASE_URL وOPENAI_API_TOKEN في Railway Variables ثم أعد النشر."
        )
        return
    status = await target.answer("🤖 جارٍ الاتصال بالمزود وقراءة النماذج المتاحة…")
    try:
        models = await provider.list_models()
    except AIProviderError as exc:
        await status.edit_text(f"تعذر الاتصال بمزود AI: {exc}")
        return
    await state.update_data(ai_models=models)
    await status.edit_text("اختر النموذج الذي تريد استخدامه:", reply_markup=_models_keyboard(models))


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
    await message.answer(text, reply_markup=enhanced_menu_keyboard())


@router.callback_query(F.data == "menu:home")
async def enhanced_home(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    if not callback.from_user or not await is_allowed(callback.from_user.id):
        await callback.answer("Private bot", show_alert=True)
        return
    if callback.message:
        try:
            await callback.message.edit_text("اختر الخدمة المطلوبة:", reply_markup=enhanced_menu_keyboard())
        except Exception:
            await callback.message.answer("اختر الخدمة المطلوبة:", reply_markup=enhanced_menu_keyboard())
    await callback.answer()


@router.message(Command("ai"))
async def ai_command(message: Message, state: FSMContext) -> None:
    if not await _allowed_message(message):
        await message.answer("هذا المستخدم غير مسموح له حاليًا.")
        return
    await _show_models(message, state)


@router.callback_query(F.data == "menu:ai")
async def ai_menu(callback: CallbackQuery, state: FSMContext) -> None:
    if not callback.from_user or not await is_allowed(callback.from_user.id):
        await callback.answer("Private bot", show_alert=True)
        return
    await callback.answer()
    if callback.message:
        await _show_models(callback.message, state)


@router.callback_query(F.data == "ai:models")
async def ai_models(callback: CallbackQuery, state: FSMContext) -> None:
    if not callback.from_user or not await is_allowed(callback.from_user.id):
        await callback.answer("Private bot", show_alert=True)
        return
    await callback.answer()
    if callback.message:
        await _show_models(callback.message, state)


@router.callback_query(F.data == "ai:new")
async def ai_new_chat(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    model = str(data.get("ai_model") or "")
    models = list(data.get("ai_models") or [])
    await state.clear()
    if model:
        await state.set_state(AIState.chatting)
        await state.update_data(ai_model=model, ai_models=models, ai_history=[])
        if callback.message:
            await callback.message.answer(f"🧹 بدأت محادثة جديدة مع {model}.", reply_markup=_chat_controls())
    elif callback.message:
        await _show_models(callback.message, state)
    await callback.answer()


@router.callback_query(F.data.startswith("aimodel:"))
async def ai_choose_model(callback: CallbackQuery, state: FSMContext) -> None:
    if not callback.from_user or not await is_allowed(callback.from_user.id):
        await callback.answer("Private bot", show_alert=True)
        return
    data = await state.get_data()
    models = list(data.get("ai_models") or [])
    try:
        index = int(callback.data.split(":", 1)[1])
        model = str(models[index])
    except (ValueError, IndexError):
        await callback.answer("أعد فتح قائمة النماذج", show_alert=True)
        return
    await state.set_state(AIState.chatting)
    await state.update_data(ai_model=model, ai_models=models, ai_history=[])
    if callback.message:
        await callback.message.answer(
            f"🤖 النموذج: {model}\nأرسل رسالتك الآن، وسأحافظ على سياق المحادثة أثناء هذه الجلسة.",
            reply_markup=_chat_controls(),
        )
    await callback.answer()


@router.message(AIState.chatting, F.text)
async def ai_chat_message(message: Message, state: FSMContext) -> None:
    if not await _allowed_message(message) or not message.text:
        return
    data = await state.get_data()
    model = str(data.get("ai_model") or "")
    history = list(data.get("ai_history") or [])
    if not model:
        await state.clear()
        await _show_models(message, state)
        return

    history.append({"role": "user", "content": message.text[:8000]})
    thinking = await message.answer("🤖 يفكر…")
    try:
        reply = await provider.chat(model, history)
    except AIProviderError as exc:
        await thinking.edit_text(f"تعذر إكمال طلب AI: {exc}")
        return

    history.append({"role": "assistant", "content": reply.text})
    history = history[-settings.ai_max_history_messages :]
    await state.update_data(ai_history=history, ai_model=reply.model or model)
    await thinking.delete()
    chunks = [reply.text[index : index + 3500] for index in range(0, len(reply.text), 3500)] or ["—"]
    for index, chunk in enumerate(chunks):
        await message.answer(chunk, reply_markup=_chat_controls() if index == len(chunks) - 1 else None)
