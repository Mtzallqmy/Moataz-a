import pytest

from app.services import runware_provider
from app.services.runware_provider import RunwareOpenAIProvider


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
    get_responses = []
    post_responses = []
    get_calls = []
    post_calls = []

    def __init__(self, *args, **kwargs):  # noqa: ARG002
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    def get(self, url, params=None):
        self.get_calls.append((url, params))
        return self.get_responses.pop(0)

    def post(self, url, json=None):
        self.post_calls.append((url, json))
        return self.post_responses.pop(0)


@pytest.fixture(autouse=True)
def reset_fake_session():
    FakeSession.get_responses = []
    FakeSession.post_responses = []
    FakeSession.get_calls = []
    FakeSession.post_calls = []


@pytest.mark.asyncio
async def test_runware_uses_public_catalog_and_air_model_ids(monkeypatch):
    monkeypatch.setattr(runware_provider.aiohttp, "ClientSession", FakeSession)
    FakeSession.get_responses = [
        FakeResponse(
            200,
            [
                {
                    "model": "sample-text-model",
                    "air": "vendor:text@1",
                    "name": "Sample Text",
                    "status": "openai-compatible",
                    "capabilities": ["io:text-to-text", "op:tool-calling"],
                }
            ],
        )
    ]
    provider = RunwareOpenAIProvider("https://api.runware.ai/v1", "placeholder-runware")

    models = await provider.list_model_infos()

    assert [model.model_id for model in models] == ["vendor:text@1"]
    assert {"text", "tools"} <= set(models[0].capabilities)
    assert FakeSession.get_calls == [
        ("https://content.runware.ai/models", {"status": "openai-compatible"})
    ]


@pytest.mark.asyncio
async def test_runware_probe_uses_account_details_without_inference(monkeypatch):
    monkeypatch.setattr(runware_provider.aiohttp, "ClientSession", FakeSession)
    FakeSession.post_responses = [
        FakeResponse(
            200,
            {
                "data": [
                    {
                        "taskType": "accountManagement",
                        "operation": "getDetails",
                    }
                ]
            },
        )
    ]
    provider = RunwareOpenAIProvider("https://api.runware.ai/v1", "placeholder-runware")

    await provider.probe_credentials()

    assert len(FakeSession.post_calls) == 1
    url, body = FakeSession.post_calls[0]
    assert url == "https://api.runware.ai/v1"
    assert body[0]["taskType"] == "accountManagement"
    assert body[0]["operation"] == "getDetails"
    assert "positivePrompt" not in str(body)
