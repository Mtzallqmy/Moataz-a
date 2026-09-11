from types import SimpleNamespace

import pytest
from aiogram.exceptions import TelegramBadRequest

from app.bot.safe_edit import safe_edit_message


@pytest.mark.asyncio
async def test_safe_edit_falls_back_to_caption_for_media_message() -> None:
    calls: list[tuple[str, str]] = []

    class MediaMessage:
        async def edit_text(self, text: str, **kwargs) -> None:
            calls.append(("text", text))
            raise TelegramBadRequest(
                method=SimpleNamespace(),
                message="there is no text in the message to edit",
            )

        async def edit_caption(self, caption: str, **kwargs) -> None:
            calls.append(("caption", caption))

    assert await safe_edit_message(MediaMessage(), "queued") is True
    assert calls == [("text", "queued"), ("caption", "queued")]


@pytest.mark.asyncio
async def test_safe_edit_treats_unchanged_message_as_success() -> None:
    class UnchangedMessage:
        async def edit_text(self, text: str, **kwargs) -> None:
            raise TelegramBadRequest(
                method=SimpleNamespace(),
                message="message is not modified",
            )

    assert await safe_edit_message(UnchangedMessage(), "same") is True
