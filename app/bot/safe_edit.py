from __future__ import annotations

import asyncio

from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError, TelegramRetryAfter
from aiogram.types import Message


async def safe_edit_message(message: Message, text: str, reply_markup=None) -> bool:
    """Edit text or media captions without allowing Telegram UI errors to break work."""

    async def edit(kind: str) -> bool | None:
        value = text if kind == "text" else text[:1024]
        method = message.edit_text if kind == "text" else message.edit_caption
        kwargs = {kind: value, "reply_markup": reply_markup}
        for attempt in range(2):
            try:
                await method(**kwargs)
                return True
            except TelegramRetryAfter as exc:
                if attempt:
                    return False
                await asyncio.sleep(min(float(exc.retry_after), 5.0))
            except TelegramBadRequest as exc:
                if "message is not modified" in str(exc).lower():
                    return True
                return None
            except TelegramNetworkError:
                return False
        return False

    edited = await edit("text")
    if edited is not None:
        return edited
    captioned = await edit("caption")
    if captioned is not None:
        return captioned
    return False
