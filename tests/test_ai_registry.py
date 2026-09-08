import pytest

from app.services import ai_registry
from app.services.ai_registry import AIProviderRegistry, ProviderSpec, load_provider_specs
from app.services.openai_compatible import ChatReply, ModelInfo


def test_load_provider_specs_supports_multiple_keys_for_same_vendor():
    env = {
        "AI_PROVIDER_ROUTER_A_NAME": "Router primary",
        "AI_PROVIDER_ROUTER_A_BASE_URL": "https://router.example/v1",
        "AI_PROVIDER_ROUTER_A_API_TOKEN": "placeholder-token-a",
        "AI_PROVIDER_ROUTER_B_NAME": "Router backup",
        "AI_PROVIDER_ROUTER_B_BASE_URL": "https://router.example/v1",
        "AI_PROVIDER_ROUTER_B_API_TOKEN": "placeholder-token-b",
    }

    specs = load_provider_specs(env)
    by_id = {spec.provider_id: spec for spec in specs}

    assert set(by_id) == {"router_a", "router_b"}
    assert by_id["router_a"].base_url == by_id["router_b"].base_url
    assert by_id["router_a"].api_token != by_id["router_b"].api_token


def test_named_presets_use_official_openai_compatible_roots():
    specs = load_provider_specs(
        {
            "OPENROUTER_API_TOKEN": "placeholder-openrouter",
            "RUNWARE_API_TOKEN": "placeholder-runware",
            "NVIDIA_API_TOKEN": "placeholder-nvidia",
            "AGENTROUTER_API_TOKEN": "placeholder-agentrouter",
            "XAI_API_TOKEN": "placeholder-xai",
            "GROQ_API_TOKEN": "placeholder-groq",
        }
    )
    by_id = {spec.provider_id: spec for spec in specs}

    assert by_id["openrouter"].base_url == "https://openrouter.ai/api/v1"
    assert by_id["runware"].base_url == "https://api.runware.ai/v1"
    assert by_id["nvidia"].base_url == "https://integrate.api.nvidia.com/v1"
    assert by_id["agentrouter"].base_url == "https://co.agentrouter.org/v1"
    assert by_id["xai"].base_url == "https://api.x.ai/v1"
    assert by_id["groq"].base_url == "https://api.groq.com/openai/v1"


@pytest.mark.asyncio
async def test_registry_aggregates_models_and_prioritizes_free(monkeypatch):
    providers = [
        ProviderSpec("one", "One", "https://one.example/v1", "placeholder-one", priority=50),
        ProviderSpec("two", "Two", "https://two.example/v1", "placeholder-two", priority=10),
    ]

    class FakeProvider:
        def __init__(self, base_url, api_token):
            self.base_url = base_url
            self.api_token = api_token

        async def list_model_infos(self):
            if "one.example" in self.base_url:
                return [
                    ModelInfo("paid-model", False, capabilities=("text", "code")),
                    ModelInfo("free-model", True, capabilities=("text",)),
                ]
            return [ModelInfo("unknown-model", None, capabilities=("text", "vision"))]

        async def chat(self, model, messages):
            return ChatReply(text=f"{self.base_url}:{messages[-1]['content']}", model=model)

    monkeypatch.setattr(ai_registry, "OpenAICompatibleProvider", FakeProvider)
    registry = AIProviderRegistry(providers)

    models, errors = await registry.list_models()

    assert errors == {}
    assert [model.model_id for model in models] == ["free-model", "unknown-model", "paid-model"]
    assert [model.provider_id for model in models] == ["one", "two", "one"]
    code_models, _ = await registry.models_for("code")
    assert [model.model_id for model in code_models] == ["paid-model"]
    vision_models, _ = await registry.models_for("vision")
    assert [model.model_id for model in vision_models] == ["unknown-model"]

    reply = await registry.chat("two", "unknown-model", [{"role": "user", "content": "hello"}])
    assert reply.model == "unknown-model"
    assert "two.example" in reply.text


def test_public_provider_metadata_never_contains_api_token():
    spec = ProviderSpec(
        provider_id="secure",
        name="Secure",
        base_url="https://secure.example/v1",
        api_token="placeholder-secret-token",
    )

    public = spec.public_dict()

    assert "api_token" not in public
    assert "placeholder-secret-token" not in str(public)
