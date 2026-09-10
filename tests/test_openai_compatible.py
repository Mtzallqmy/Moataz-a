import pytest
from pydantic import ValidationError

from app.config import Settings
from app.security import redact_secrets
from app.services.openai_compatible import AIProviderError, OpenAICompatibleProvider


def test_optional_ai_settings_enable_provider_without_becoming_core_requirements():
    base = Settings(bot_token="placeholder", database_url="sqlite+aiosqlite:///:memory:")
    assert base.ai_enabled is False

    configured = Settings(
        bot_token="placeholder",
        database_url="sqlite+aiosqlite:///:memory:",
        openai_base_url="https://provider.example/v1/",
        openai_api_token="placeholder-api-token-123456",
    )
    assert configured.openai_base_url == "https://provider.example/v1"
    assert configured.ai_enabled is True


@pytest.mark.parametrize(
    "url",
    [
        "ftp://provider.example/v1",
        "https://user:pass@provider.example/v1",
        "https://provider.example/v1#fragment",
        "provider.example/v1",
    ],
)
def test_ai_base_url_validation_rejects_unsafe_or_malformed_values(url):
    with pytest.raises(ValidationError):
        Settings(openai_base_url=url)


class StubProvider(OpenAICompatibleProvider):
    def __init__(self, responses):
        super().__init__("https://provider.example/v1", "placeholder-api-token-123456")
        self.responses = responses
        self.calls = []

    async def _request_json(self, method, path, *, json_body=None):
        self.calls.append((method, path, json_body))
        response = self.responses[path]
        if isinstance(response, Exception):
            raise response
        return response


@pytest.mark.asyncio
async def test_openai_compatible_models_and_chat_use_standard_paths():
    provider = StubProvider(
        {
            "models": {"data": [{"id": "model-b"}, {"id": "model-a"}]},
            "chat/completions": {
                "model": "model-a",
                "choices": [{"message": {"content": "مرحبا من المزود"}}],
            },
        }
    )

    models = await provider.list_models()
    reply = await provider.chat("model-a", [{"role": "user", "content": "مرحبا"}])

    assert models == ["model-a", "model-b"]
    assert reply.text == "مرحبا من المزود"
    assert reply.model == "model-a"
    assert provider.calls[0] == ("GET", "models", None)
    method, path, body = provider.calls[1]
    assert method == "POST"
    assert path == "chat/completions"
    assert body == {
        "model": "model-a",
        "messages": [{"role": "user", "content": "مرحبا"}],
        "stream": False,
    }


@pytest.mark.asyncio
async def test_openai_compatible_rejects_invalid_chat_payload():
    provider = StubProvider({"chat/completions": {"choices": []}})
    with pytest.raises(AIProviderError):
        await provider.chat("model-a", [{"role": "user", "content": "hello"}])


@pytest.mark.asyncio
async def test_openai_compatible_parses_native_tool_calls_without_executing_them():
    provider = StubProvider(
        {
            "chat/completions": {
                "model": "tool-model",
                "choices": [
                    {
                        "message": {
                            "content": "سأضبط المقاس",
                            "tool_calls": [
                                {
                                    "function": {
                                        "name": "set_canvas",
                                        "arguments": '{"preset":"9:16"}',
                                    }
                                }
                            ],
                        }
                    }
                ],
            }
        }
    )
    tools = [{"type": "function", "function": {"name": "set_canvas"}}]
    reply = await provider.chat_tools(
        "tool-model", [{"role": "user", "content": "Reels"}], tools
    )
    assert reply.tool_calls == (({"name": "set_canvas", "arguments": {"preset": "9:16"}}),)
    body = provider.calls[0][2]
    assert body["tools"] == tools
    assert body["tool_choice"] == "auto"


def test_ai_token_is_redacted_from_errors_and_authorization_text():
    token = "placeholder-api-token-123456"
    provider = OpenAICompatibleProvider("https://provider.example/v1", token)
    error = provider._error_message(401, {"error": {"message": f"bad token {token}"}})
    assert token not in error
    assert "[REDACTED]" in error

    redacted = redact_secrets(f"Authorization: Bearer {token}", api_token=token)
    assert token not in redacted
