from aiogram import Bot
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer

from app.config import get_settings

settings = get_settings()


def create_bot() -> Bot:
    if settings.telegram_local_api_url:
        api = TelegramAPIServer.from_base(
            settings.telegram_local_api_url.rstrip("/"),
            is_local=True,
        )
        session = AiohttpSession(api=api)
        return Bot(settings.bot_token, session=session)
    return Bot(settings.bot_token)
