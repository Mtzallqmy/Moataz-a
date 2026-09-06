from functools import lru_cache

from aiogram import Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage


@lru_cache(maxsize=1)
def create_dispatcher() -> Dispatcher:
    """Return one dispatcher so a module reload cannot re-attach the same Router."""
    from app.bot.handlers import router

    dispatcher = Dispatcher(storage=MemoryStorage())
    dispatcher.include_router(router)
    return dispatcher
