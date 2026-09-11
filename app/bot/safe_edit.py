from __future__ import annotations

from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError
from aiogram.types import Message


async def safe_edit_message(message: Message, text: str, reply_markup=None) -> bool:
    """Edit text or media captions without allowing Telegram UI errors to break work."""

    try:
        await message.edit_text(text, reply_markup=reply_markup)
        return True
    except TelegramBadRequest as exc:
        if "message is not modified" in str(exc).lower():
            return True
    except TelegramNetworkError:
        return False
    try:
        await message.edit_caption(caption=text[:1024], reply_markup=reply_markup)
        return True
    except TelegramBadRequest as exc:
        return "message is not modified" in str(exc).lower()
    except TelegramNetworkError:
        return False
