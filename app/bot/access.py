from sqlalchemy import select

from app.config import get_settings
from app.db import SessionLocal, User

settings = get_settings()


async def is_allowed(telegram_id: int) -> bool:
    if telegram_id in settings.admin_ids or telegram_id in settings.allowed_ids:
        return True
    async with SessionLocal() as session:
        user = await session.scalar(select(User).where(User.telegram_id == telegram_id))
        return bool(user and user.is_allowed)


async def ensure_user(telegram_id: int, username: str | None = None) -> User:
    async with SessionLocal() as session:
        user = await session.scalar(select(User).where(User.telegram_id == telegram_id))
        if user is None:
            user = User(
                telegram_id=telegram_id,
                username=username,
                language=settings.default_language,
                is_allowed=(
                    telegram_id in settings.allowed_ids
                    or telegram_id in settings.admin_ids
                ),
                is_admin=telegram_id in settings.admin_ids,
            )
            session.add(user)
        else:
            user.username = username
            if telegram_id in settings.admin_ids:
                user.is_admin = True
                user.is_allowed = True
            elif telegram_id in settings.allowed_ids:
                user.is_allowed = True
        await session.commit()
        await session.refresh(user)
        return user
