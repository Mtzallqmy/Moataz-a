from __future__ import annotations

import asyncio
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import urlsplit

from app.config import get_settings
from app.services.openai_compatible import AIProviderError, ChatReply, OpenAICompatibleProvider

_PROVIDER_ENV_RE = re.compile(
    r"^AI_PROVIDER_([A-Z0-9][A-Z0-9_]*)_(NAME|BASE_URL|API_TOKEN|API_KEY|PRIORITY)$"
)

_PRESETS = (
    (
        "openrouter",
        "OpenRouter",
        ("OPENROUTER_API_TOKEN", "OPENROUTER_API_KEY"),
        "OPENROUTER_BASE_URL",
        "https://openrouter.ai/api/v1",
        20,
    ),
    (
        "nvidia",
        "NVIDIA",
        ("NVIDIA_API_TOKEN", "NVIDIA_API_KEY"),
        "NVIDIA_BASE_URL",
        "https://integrate.api.nvidia.com/v1",
        30,
    ),
    (
        "xai",
        "Grok (xAI)",
        ("XAI_API_TOKEN", "XAI_API_KEY"),
        "XAI_BASE_URL",
        "https://api.x.ai/v1",
        40,
    ),
    (
        "groq",
        "Groq",
        ("GROQ_API_TOKEN", "GROQ_API_KEY"),
        "GROQ_BASE_URL",
        "https://api.groq.com/openai/v1",
        45,
    ),
)


@dataclass(frozen=True, slots=True)
class ProviderSpec:
    provider_id: str
    name: str
    base_url: str
    api_token: str
    priority: int = 100

    def public_dict(self) -> dict[str, str | int]:
        return {
            "provider_id": self.provider_id,
            "name": self.name,
            "base_url": self.base_url,
            "priority": self.priority,
        }


@dataclass(frozen=True, slots=True)
class CatalogModel:
    provider_id: str
    provider_name: str
    model_id: str
    is_free: bool | None = None
    priority: int = 100

    def state_dict(self) -> dict[str, str | bool | int | None]:
        return {
            "provider_id": self.provider_id,
            "provider_name": self.provider_name,
            "model_id": self.model_id,
            "is_free": self.is_free,
            "priority": self.priority,
        }


def _normalize_base_url(value: object) -> str:
    url = str(value or "").strip().rstrip("/")
    if not url:
        return ""
    try:
        parsed = urlsplit(url)
    except ValueError:
        return ""
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        return ""
    if parsed.username or parsed.password or parsed.fragment:
        return ""
    return url


def _slug(value: str) -> str:
    cleaned = re.sub(r"[^a-z0-9_]+", "_", value.lower()).strip("_")
    return cleaned[:48] or "provider"


def _infer_name(base_url: str) -> str:
    host = (urlsplit(base_url).hostname or "").lower()
    if "tokenrouter.com" in host:
        return "TokenRouter"
    if "bynara.id" in host:
        return "Nara Router"
    if "openrouter.ai" in host:
        return "OpenRouter"
    if "nvidia.com" in host:
        return "NVIDIA"
    if host.endswith("x.ai"):
        return "Grok (xAI)"
    if "groq.com" in host:
        return "Groq"
    return host or "OpenAI-compatible"


def _first_value(env: Mapping[str, str], keys: tuple[str, ...]) -> str:
    for key in keys:
        value = str(env.get(key) or "").strip()
        if value:
            return value
    return ""


def _priority(value: object, default: int) -> int:
    try:
        return max(0, min(int(str(value)), 1000))
    except (TypeError, ValueError):
        return default


def load_provider_specs(env: Mapping[str, str] | None = None) -> list[ProviderSpec]:
    """Build provider configs without ever serializing API tokens.

    Arbitrary providers use AI_PROVIDER_<ID>_{NAME,BASE_URL,API_TOKEN,PRIORITY}.
    Different IDs may point at the same base URL, so multiple keys for one vendor
    are supported intentionally.
    """

    source = os.environ if env is None else env
    specs: list[ProviderSpec] = []
    buckets: dict[str, dict[str, str]] = {}

    for key, raw_value in source.items():
        match = _PROVIDER_ENV_RE.fullmatch(str(key).upper())
        if match is None:
            continue
        provider_key, field = match.groups()
        buckets.setdefault(provider_key, {})[field] = str(raw_value or "").strip()

    for provider_key in sorted(buckets):
        values = buckets[provider_key]
        base_url = _normalize_base_url(values.get("BASE_URL"))
        api_token = values.get("API_TOKEN") or values.get("API_KEY") or ""
        if not base_url or not api_token:
            continue
        provider_id = _slug(provider_key)
        specs.append(
            ProviderSpec(
                provider_id=provider_id,
                name=values.get("NAME") or provider_key.replace("_", " ").title(),
                base_url=base_url,
                api_token=api_token,
                priority=_priority(values.get("PRIORITY"), 100),
            )
        )

    for provider_id, name, token_keys, base_key, default_base, default_priority in _PRESETS:
        token = _first_value(source, token_keys)
        if not token:
            continue
        base_url = _normalize_base_url(source.get(base_key) or default_base)
        if not base_url:
            continue
        specs.append(
            ProviderSpec(
                provider_id=provider_id,
                name=name,
                base_url=base_url,
                api_token=token,
                priority=default_priority,
            )
        )

    if env is None:
        settings = get_settings()
        legacy_base = _normalize_base_url(settings.openai_base_url)
        legacy_token = settings.openai_api_token.strip()
    else:
        legacy_base = _normalize_base_url(source.get("OPENAI_BASE_URL"))
        legacy_token = str(source.get("OPENAI_API_TOKEN") or "").strip()
    if legacy_base and legacy_token:
        specs.append(
            ProviderSpec(
                provider_id="legacy_openai",
                name=_infer_name(legacy_base),
                base_url=legacy_base,
                api_token=legacy_token,
                priority=50,
            )
        )

    deduped: list[ProviderSpec] = []
    seen_credentials: set[tuple[str, str]] = set()
    seen_ids: set[str] = set()
    for spec in sorted(specs, key=lambda item: (item.priority, item.name.lower(), item.provider_id)):
        credential_key = (spec.base_url, spec.api_token)
        if credential_key in seen_credentials:
            continue
        provider_id = spec.provider_id
        if provider_id in seen_ids:
            suffix = 2
            while f"{provider_id}_{suffix}" in seen_ids:
                suffix += 1
            spec = ProviderSpec(
                provider_id=f"{provider_id}_{suffix}",
                name=spec.name,
                base_url=spec.base_url,
                api_token=spec.api_token,
                priority=spec.priority,
            )
        seen_credentials.add(credential_key)
        seen_ids.add(spec.provider_id)
        deduped.append(spec)
    return deduped


class AIProviderRegistry:
    def __init__(self, providers: list[ProviderSpec] | None = None) -> None:
        self.providers = list(load_provider_specs() if providers is None else providers)
        self._by_id = {provider.provider_id: provider for provider in self.providers}

    @property
    def enabled(self) -> bool:
        return bool(self.providers)

    def provider(self, provider_id: str) -> ProviderSpec:
        try:
            return self._by_id[provider_id]
        except KeyError as exc:
            raise AIProviderError("AI provider is no longer configured") from exc

    async def list_models(self) -> tuple[list[CatalogModel], dict[str, str]]:
        async def fetch(spec: ProviderSpec) -> tuple[ProviderSpec, object]:
            client = OpenAICompatibleProvider(spec.base_url, spec.api_token)
            try:
                return spec, await client.list_model_infos()
            except AIProviderError as exc:
                return spec, exc

        results = await asyncio.gather(*(fetch(spec) for spec in self.providers))
        models: list[CatalogModel] = []
        errors: dict[str, str] = {}
        for spec, result in results:
            if isinstance(result, AIProviderError):
                errors[spec.provider_id] = str(result)[:240]
                continue
            for model in result:
                models.append(
                    CatalogModel(
                        provider_id=spec.provider_id,
                        provider_name=spec.name,
                        model_id=model.model_id,
                        is_free=model.is_free,
                        priority=spec.priority,
                    )
                )

        unique: dict[tuple[str, str], CatalogModel] = {
            (model.provider_id, model.model_id): model for model in models
        }
        ordered = sorted(
            unique.values(),
            key=lambda model: (
                0 if model.is_free is True else 1,
                model.priority,
                model.provider_name.lower(),
                model.model_id.lower(),
            ),
        )
        return ordered, errors

    async def chat(self, provider_id: str, model: str, messages: list[dict[str, str]]) -> ChatReply:
        spec = self.provider(provider_id)
        client = OpenAICompatibleProvider(spec.base_url, spec.api_token)
        return await client.chat(model, messages)


_registry: AIProviderRegistry | None = None


def get_ai_provider_registry() -> AIProviderRegistry:
    global _registry
    if _registry is None:
        _registry = AIProviderRegistry()
    return _registry
