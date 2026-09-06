from sqlalchemy import select

from app.config import get_settings
from app.db import SessionLocal, User

settings = get_settings()


async def is_allowed(telegram_id: int) -> bool:
    async with SessionLocal() as session:
        user = await session.scalar(select(User).where(User.telegram_id == telegram_id))
        return True if user is None else bool(user.is_allowed)


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
        await session.commit()
        await session.refresh(user)
        return user
