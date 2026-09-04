from aiogram import Bot
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import PRODUCTION

from app.config import get_settings

settings = get_settings()


def create_bot() -> Bot:
    """Create a bot session pinned to Telegram's official production API.

    The API server is supplied explicitly so stale Railway variables or previous
    local Bot API configuration cannot replace https://api.telegram.org.
    """
    session = AiohttpSession(api=PRODUCTION)
    return Bot(token=settings.bot_token, session=session)
