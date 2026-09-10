from functools import lru_cache

from aiogram import Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage


@lru_cache(maxsize=1)
def create_dispatcher() -> Dispatcher:
    """Return one dispatcher so a module reload cannot re-attach the same Router."""
    from app.bot.advanced_media import router as advanced_media_router
    from app.bot.ai import router as ai_router
    from app.bot.ai_hub import router as ai_hub_router
    from app.bot.ai_multimodal import router as ai_multimodal_router
    from app.bot.features import router as feature_router
    from app.bot.handlers import router as handlers_router
    from app.bot.state_recovery import router as state_recovery_router
    from app.bot.studio import router as studio_router

    dispatcher = Dispatcher(storage=MemoryStorage())
    dispatcher.include_router(advanced_media_router)
    dispatcher.include_router(ai_hub_router)
    dispatcher.include_router(ai_multimodal_router)
    dispatcher.include_router(ai_router)
    dispatcher.include_router(state_recovery_router)
    dispatcher.include_router(studio_router)
    dispatcher.include_router(feature_router)
    dispatcher.include_router(handlers_router)
    return dispatcher
