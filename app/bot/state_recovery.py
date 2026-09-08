from __future__ import annotations

from aiogram import Router
from aiogram.filters import BaseFilter, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from app.bot.features import handle_media_text
from app.bot.handlers import CutState, start
from app.services.urls import parse_bulk_urls

router = Router(name="state-recovery")
analyze_text = handle_media_text


class ContainsMediaURL(BaseFilter):
    async def __call__(self, message: Message) -> bool:
        return bool(message.text and parse_bulk_urls(message.text).urls)


@router.message(CutState.waiting_range, CommandStart())
async def reset_stale_cut_state_on_start(message: Message, state: FSMContext) -> None:
    """Treat /start as a hard reset for any interrupted interactive flow."""
    await state.clear()
    await start(message)


@router.message(CutState.waiting_range, ContainsMediaURL())
async def recover_url_from_stale_cut_state(message: Message, state: FSMContext) -> None:
    """Clear an abandoned legacy cut state and use the current media flow."""
    await state.clear()
    await analyze_text(message)
