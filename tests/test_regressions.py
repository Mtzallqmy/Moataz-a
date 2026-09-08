from pathlib import Path
from types import SimpleNamespace

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


def test_env_example_documents_core_and_multi_provider_ai_variables():
    raw_lines = [line.strip() for line in Path(".env.example").read_text().splitlines()]
    variables = [line for line in raw_lines if line and not line.startswith("#")]
    names = {line.split("=", 1)[0] for line in variables}

    assert {"BOT_TOKEN", "DATABASE_URL", "OPENAI_BASE_URL", "OPENAI_API_TOKEN"} <= names
    assert {
        "OPENROUTER_BASE_URL",
        "OPENROUTER_API_TOKEN",
        "NVIDIA_BASE_URL",
        "NVIDIA_API_TOKEN",
        "XAI_BASE_URL",
        "XAI_API_TOKEN",
        "GROQ_BASE_URL",
        "GROQ_API_TOKEN",
        "AI_PROVIDER_EXAMPLE_1_NAME",
        "AI_PROVIDER_EXAMPLE_1_BASE_URL",
        "AI_PROVIDER_EXAMPLE_1_API_TOKEN",
        "AI_PROVIDER_EXAMPLE_1_PRIORITY",
    } <= names

    token_lines = [line for line in variables if line.split("=", 1)[0].endswith(("TOKEN", "API_KEY"))]
    assert all(line.endswith("=") for line in token_lines)


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
async def test_url_breaks_stale_cut_state_and_uses_normal_analyzer(monkeypatch):
    pytest.importorskip("aiogram")
    from app.bot import state_recovery

    message = SimpleNamespace(text="https://www.facebook.com/share/v/example/")
    calls = []

    class FakeState:
        cleared = False

        async def clear(self):
            self.cleared = True

    state = FakeState()

    async def fake_analyze(received_message):
        calls.append(received_message)

    monkeypatch.setattr(state_recovery, "analyze_text", fake_analyze)

    url_filter = state_recovery.ContainsMediaURL()
    assert await url_filter(message) is True
    assert await url_filter(SimpleNamespace(text="00:10 - 00:20")) is False

    await state_recovery.recover_url_from_stale_cut_state(message, state)

    assert state.cleared is True
    assert calls == [message]


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
