from sqlalchemy import select

from app.config import get_settings
from app.db import SessionLocal, User

settings = get_settings()


async def is_allowed(telegram_id: int) -> bool:
    # With no access list configured the bot is open. This keeps BOT_TOKEN + DATABASE_URL
    # sufficient for first deployment. Configure ADMIN_TELEGRAM_IDS/ALLOWED_TELEGRAM_IDS
    # later to make the bot private.
    if not settings.access_restricted:
        return True
    if telegram_id in settings.admin_ids or telegram_id in settings.allowed_ids:
        return True
    async with SessionLocal() as session:
        user = await session.scalar(select(User).where(User.telegram_id == telegram_id))
        return bool(user and user.is_allowed)


async def ensure_user(telegram_id: int, username: str | None = None) -> User:
    async with SessionLocal() as session:
        user = await session.scalar(select(User).where(User.telegram_id == telegram_id))
        default_allowed = (
            not settings.access_restricted
            or telegram_id in settings.allowed_ids
            or telegram_id in settings.admin_ids
        )
        if user is None:
            user = User(
                telegram_id=telegram_id,
                username=username,
                language=settings.default_language,
                is_allowed=default_allowed,
                is_admin=telegram_id in settings.admin_ids,
            )
            session.add(user)
        else:
            user.username = username
            if not settings.access_restricted:
                user.is_allowed = True
            if telegram_id in settings.admin_ids:
                user.is_admin = True
                user.is_allowed = True
            elif telegram_id in settings.allowed_ids:
                user.is_allowed = True
        await session.commit()
        await session.refresh(user)
        return user
