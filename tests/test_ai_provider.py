import pytest

from app.services import openai_compatible
from app.services.openai_compatible import AIProviderError, OpenAICompatibleProvider


class FakeResponse:
    def __init__(self, status, payload):
        self.status = status
        self.payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def json(self, content_type=None):  # noqa: ARG002
        return self.payload


class FakeSession:
    responses = []
    calls = []

    def __init__(self, *args, **kwargs):  # noqa: ARG002
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    def request(self, method, url, json=None):
        self.calls.append((method, url, json))
        return self.responses.pop(0)


@pytest.fixture(autouse=True)
def reset_fake_session():
    FakeSession.responses = []
    FakeSession.calls = []


@pytest.mark.asyncio
async def test_lists_models_from_openai_compatible_endpoint(monkeypatch):
    monkeypatch.setattr(openai_compatible.aiohttp, "ClientSession", FakeSession)
    FakeSession.responses = [FakeResponse(200, {"data": [{"id": "model-b"}, {"id": "model-a"}]})]
    provider = OpenAICompatibleProvider("https://provider.example/v1", "placeholder-token")

    models = await provider.list_models()

    assert models == ["model-a", "model-b"]
    assert FakeSession.calls[0][1] == "https://provider.example/v1/models"


@pytest.mark.asyncio
async def test_chat_uses_selected_model_and_message_history(monkeypatch):
    monkeypatch.setattr(openai_compatible.aiohttp, "ClientSession", FakeSession)
    FakeSession.responses = [
        FakeResponse(
            200,
            {
                "model": "model-a",
                "choices": [{"message": {"role": "assistant", "content": "Hello from provider"}}],
            },
        )
    ]
    provider = OpenAICompatibleProvider("https://provider.example/v1", "placeholder-token")

    reply = await provider.chat("model-a", [{"role": "user", "content": "Hello"}])

    assert reply.text == "Hello from provider"
    assert reply.model == "model-a"
    method, url, body = FakeSession.calls[0]
    assert method == "POST"
    assert url == "https://provider.example/v1/chat/completions"
    assert body["model"] == "model-a"
    assert body["messages"] == [{"role": "user", "content": "Hello"}]
    assert body["stream"] is False


@pytest.mark.asyncio
async def test_provider_surfaces_compatible_api_errors_without_token(monkeypatch):
    monkeypatch.setattr(openai_compatible.aiohttp, "ClientSession", FakeSession)
    FakeSession.responses = [FakeResponse(401, {"error": {"message": "invalid token"}})]
    provider = OpenAICompatibleProvider("https://provider.example/v1", "placeholder-token")

    with pytest.raises(AIProviderError, match="HTTP 401") as exc_info:
        await provider.list_models()

    assert "placeholder-token" not in str(exc_info.value)


def test_provider_requires_both_base_url_and_token():
    assert OpenAICompatibleProvider("", "token").enabled is False
    assert OpenAICompatibleProvider("https://provider.example/v1", "").enabled is False
    assert OpenAICompatibleProvider("https://provider.example/v1", "token").enabled is True
