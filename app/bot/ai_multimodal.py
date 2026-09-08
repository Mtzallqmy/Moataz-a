from __future__ import annotations

import base64
import io

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from app.bot.access import ensure_user, is_allowed
from app.bot.ai import AIState, _chat_controls, _show_models
from app.config import get_settings
from app.services.ai_registry import get_ai_provider_registry
from app.services.openai_compatible import AIProviderError

settings = get_settings()
registry = get_ai_provider_registry()
router = Router(name="ai-multimodal")
_MAX_IMAGE_BYTES = 8 * 1024 * 1024


def _known_selected_model(data: dict[str, object]) -> dict[str, object] | None:
    provider_id = str(data.get("ai_provider_id") or "")
    model_id = str(data.get("ai_model") or "")
    for item in list(data.get("ai_models") or []):
        if not isinstance(item, dict):
            continue
        if str(item.get("provider_id") or "") == provider_id and str(item.get("model_id") or "") == model_id:
            return item
    return None


def _supports_vision(model: dict[str, object] | None) -> bool | None:
    if model is None:
        return None
    capabilities = {str(item) for item in model.get("capabilities") or []}
    inputs = {str(item) for item in model.get("input_modalities") or []}
    return "vision" in capabilities or "image" in inputs


@router.message(AIState.chatting, F.photo)
async def ai_chat_photo(message: Message, state: FSMContext) -> None:
    if not message.from_user or not message.photo or not await is_allowed(message.from_user.id):
        return
    user = await ensure_user(message.from_user.id, message.from_user.username)
    data = await state.get_data()
    provider_id = str(data.get("ai_provider_id") or "")
    provider_name = str(data.get("ai_provider_name") or provider_id)
    model_id = str(data.get("ai_model") or "")
    history = list(data.get("ai_history") or [])
    if not provider_id or not model_id:
        await state.clear()
        await _show_models(message, state, user.language)
        return

    known_model = _known_selected_model(data)
    if _supports_vision(known_model) is False:
        await message.answer(
            "This model does not advertise image input. Choose a model from 👁️ Vision / image input."
            if user.language == "en"
            else "هذا النموذج لا يعلن دعم إدخال الصور. اختر نموذجًا من قسم 👁️ الرؤية / إدخال الصور.",
            reply_markup=_chat_controls(user.language),
        )
        return

    photo = message.photo[-1]
    if photo.file_size and photo.file_size > _MAX_IMAGE_BYTES:
        await message.answer(
            "The image is too large for AI input." if user.language == "en" else "الصورة كبيرة جدًا لإرسالها إلى نموذج AI."
        )
        return
    if message.bot is None:
        await message.answer("Telegram client is unavailable." if user.language == "en" else "عميل Telegram غير متاح حاليًا.")
        return

    buffer = io.BytesIO()
    try:
        await message.bot.download(photo, destination=buffer)
    except Exception as exc:
        await message.answer(
            f"Could not read the image: {type(exc).__name__}"
            if user.language == "en"
            else f"تعذر قراءة الصورة: {type(exc).__name__}"
        )
        return
    raw = buffer.getvalue()
    if len(raw) > _MAX_IMAGE_BYTES:
        await message.answer("The image is too large." if user.language == "en" else "الصورة كبيرة جدًا.")
        return

    caption = (message.caption or "").strip()
    if not caption:
        caption = "Describe and analyze this image." if user.language == "en" else "حلّل هذه الصورة واشرح محتواها."
    encoded = base64.b64encode(raw).decode("ascii")
    multimodal_message = {
        "role": "user",
        "content": [
            {"type": "text", "text": caption[:4000]},
            {
                "type": "image_url",
                "image_url": {"url": f"data:image/jpeg;base64,{encoded}"},
            },
        ],
    }
    request_history = history + [multimodal_message]
    thinking = await message.answer(
        "👁️ Analyzing image…" if user.language == "en" else "👁️ جارٍ تحليل الصورة…"
    )
    try:
        reply = await registry.chat(provider_id, model_id, request_history)
    except AIProviderError as exc:
        safe = str(exc)[:300]
        await thinking.edit_text(
            f"Vision request failed: {safe}"
            if user.language == "en"
            else f"تعذر تحليل الصورة بهذا النموذج: {safe}",
            reply_markup=_chat_controls(user.language),
        )
        return

    history.append({"role": "user", "content": f"[Image] {caption[:2000]}"})
    history.append({"role": "assistant", "content": reply.text})
    history = history[-settings.ai_max_history_messages :]
    await state.update_data(
        ai_history=history,
        ai_provider_id=provider_id,
        ai_provider_name=provider_name,
        ai_model=reply.model or model_id,
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
