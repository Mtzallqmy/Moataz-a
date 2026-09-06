from pathlib import Path

import pytest


def test_pyproject_has_no_redis_or_arq_runtime_dependency():
    text = Path("pyproject.toml").read_text()
    assert '"redis' not in text.lower()
    assert '"arq' not in text.lower()


def test_railway_uses_single_service_startup():
    text = Path("railway.json").read_text()
    assert "python -m app.main" in text
    assert "worker" not in text.lower()
    assert "redis" not in text.lower()


def test_env_example_only_contains_basic_required_variables():
    lines = [line.strip() for line in Path(".env.example").read_text().splitlines() if line.strip()]
    assert lines == ["BOT_TOKEN=", "DATABASE_URL="]


def test_router_can_be_requested_repeatedly_without_already_attached_error():
    pytest.importorskip("aiogram")
    from app.bot import create_dispatcher

    first = create_dispatcher()
    second = create_dispatcher()
    assert first is second


def test_telegram_client_is_pinned_to_official_production_api():
    pytest.importorskip("aiogram")
    from app.bot.client import create_bot

    bot = create_bot()
    assert "api.telegram.org" in bot.session.api.base


@pytest.mark.asyncio
async def test_delete_webhook_failure_does_not_prevent_polling(monkeypatch):
    pytest.importorskip("aiogram")
    from app import main

    calls = {"poll": 0}

    class FakeBot:
        async def delete_webhook(self, **kwargs):  # noqa: ARG002
            raise RuntimeError("temporary Telegram error")

    class FakeDispatcher:
        def resolve_used_update_types(self):
            return []

        async def start_polling(self, *args, **kwargs):  # noqa: ARG002
            calls["poll"] += 1
            raise __import__("asyncio").CancelledError

    monkeypatch.setattr(main, "dispatcher", FakeDispatcher())
    with pytest.raises(__import__("asyncio").CancelledError):
        await main._run_polling_forever(FakeBot())
    assert calls["poll"] == 1
