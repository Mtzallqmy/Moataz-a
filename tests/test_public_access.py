import pytest

from app.bot.access import is_allowed


@pytest.mark.asyncio
async def test_every_real_telegram_user_is_allowed():
    assert await is_allowed(1) is True
    assert await is_allowed(123456789) is True
    assert await is_allowed(9_999_999_999) is True


@pytest.mark.asyncio
async def test_non_user_sentinel_is_not_allowed():
    assert await is_allowed(0) is False
    assert await is_allowed(-1) is False
