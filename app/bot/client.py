from aiogram import Bot
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import PRODUCTION, TelegramAPIServer

from app.config import get_settings


def create_bot() -> Bot:
    settings = get_settings()
    api = TelegramAPIServer.from_base(f"http://127.0.0.1:{settings.telegram_local_api_port}", is_local=True) if settings.telegram_local_api_enabled else PRODUCTION
    session = AiohttpSession(api=api)
    return Bot(token=settings.bot_token, session=session)
