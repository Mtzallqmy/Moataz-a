from sqlalchemy import select

from app.config import get_settings
from app.db import SessionLocal, User

settings = get_settings()


async def is_allowed(telegram_id: int) -> bool:
    """Allow every real Telegram user to use the bot.

    Access is intentionally public. Existing database flags are no longer used
    as an allow-list gate; they are normalized back to allowed when the user is
    seen again so old private-mode records cannot block returning users.
    """
    return int(telegram_id) > 0


async def ensure_user(telegram_id: int, username: str | None = None) -> User:
    async with SessionLocal() as session:
        user = await session.scalar(select(User).where(User.telegram_id == telegram_id))
        if user is None:
            user = User(
                telegram_id=telegram_id,
                username=username,
                language=settings.default_language,
                is_allowed=True,
                is_admin=False,
            )
            session.add(user)
        else:
            user.username = username
            user.is_allowed = True
        await session.commit()
        await session.refresh(user)
        return user
