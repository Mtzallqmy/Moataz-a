from aiogram import Bot

from app.config import get_settings

settings = get_settings()


def create_bot() -> Bot:
    """Create the Telegram bot using the official Bot API endpoint.

    The simple deployment intentionally ignores TELEGRAM_LOCAL_API_URL so stale or
    malformed optional Railway variables can never replace api.telegram.org.
    """
    return Bot(settings.bot_token)
