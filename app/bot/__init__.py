from functools import lru_cache

from aiogram import Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage


@lru_cache(maxsize=1)
def create_dispatcher() -> Dispatcher:
    """Return one dispatcher so a module reload cannot re-attach the same Router."""
    from app.bot.features import router as feature_router
    from app.bot.handlers import router as handlers_router
    from app.bot.state_recovery import router as state_recovery_router

    dispatcher = Dispatcher(storage=MemoryStorage())
    dispatcher.include_router(state_recovery_router)
    dispatcher.include_router(feature_router)
    dispatcher.include_router(handlers_router)
    return dispatcher
