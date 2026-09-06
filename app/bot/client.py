from aiogram import Bot
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import PRODUCTION

from app.config import get_settings


def create_bot() -> Bot:
    settings = get_settings()
    session = AiohttpSession(api=PRODUCTION)
    return Bot(token=settings.bot_token, session=session)
