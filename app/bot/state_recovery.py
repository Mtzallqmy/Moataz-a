from __future__ import annotations

from aiogram import Router
from aiogram.filters import BaseFilter, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from app.bot.handlers import CutState, analyze_text, start
from app.services.urls import parse_bulk_urls

router = Router(name="state-recovery")


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
    """Never parse an HTTP/HTTPS URL as a cut range.

    A user can leave the cut FSM active by abandoning a previous interaction. If
    their next message is a URL, clear the stale state and send the message to
    the normal analyzer instead of returning the misleading cut-range error.
    """
    await state.clear()
    await analyze_text(message)
