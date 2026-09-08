from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

import aiohttp

from app.config import get_settings
from app.security import redact_secrets
from app.services.model_capabilities import extract_capabilities, extract_modalities

settings = get_settings()
_FREE_MODEL_TOKEN = re.compile(r"(?:^|[/:._-])free(?:$|[/:._-])", re.IGNORECASE)


class AIProviderError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ChatReply:
    text: str
    model: str


@dataclass(frozen=True, slots=True)
class ModelInfo:
    model_id: str
    is_free: bool | None = None
    capabilities: tuple[str, ...] = ()
    input_modalities: tuple[str, ...] = ()
    output_modalities: tuple[str, ...] = ()


class OpenAICompatibleProvider:
    """Minimal OpenAI-compatible client without an SDK dependency.

    The configured base URL is treated as the API root, normally ending in
    `/v1`. `/models` is used for discovery and `/chat/completions` for chat.
    """

    def __init__(self, base_url: str | None = None, api_token: str | None = None) -> None:
        self.base_url = (base_url if base_url is not None else settings.openai_base_url).rstrip("/")
        self.api_token = (api_token if api_token is not None else settings.openai_api_token).strip()

    @property
    def enabled(self) -> bool:
        return bool(self.base_url and self.api_token)

    def _headers(self) -> dict[str, str]:
        if not self.enabled:
            raise AIProviderError("OpenAI-compatible provider is not configured")
        return {
            "Authorization": f"Bearer {self.api_token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def _safe(self, value: object) -> str:
        return redact_secrets(value, api_token=self.api_token)

    async def _request_json(
        self,
        method: str,
        path: str,
        *,
        json_body: object | None = None,
    ) -> object:
        if not self.enabled:
            raise AIProviderError("OpenAI-compatible provider is not configured")
        timeout = aiohttp.ClientTimeout(total=settings.ai_request_timeout_seconds)
        url = f"{self.base_url}/{path.lstrip('/')}"
        try:
            async with aiohttp.ClientSession(timeout=timeout, headers=self._headers()) as session:
                async with session.request(method, url, json=json_body) as response:
                    try:
                        payload = await response.json(content_type=None)
                    except (ValueError, TypeError) as exc:
                        raise AIProviderError(f"AI provider HTTP {response.status}: invalid JSON response") from exc
                    if response.status >= 400:
                        raise AIProviderError(self._error_message(response.status, payload))
                    return payload
        except TimeoutError as exc:
            raise AIProviderError("AI provider request timed out") from exc
        except aiohttp.ClientError as exc:
            raise AIProviderError(f"AI provider network error: {type(exc).__name__}") from exc

    @staticmethod
    def _number(value: object) -> float | None:
        if value is None or isinstance(value, bool):
            return None
        try:
            return float(str(value))
        except (TypeError, ValueError):
            return None

    @classmethod
    def _is_free_model(cls, item: dict[str, Any]) -> bool | None:
        model_id = str(item.get("id") or "").lower()
        if _FREE_MODEL_TOKEN.search(model_id):
            return True

        for key in ("is_free", "free"):
            flag = item.get(key)
            if isinstance(flag, bool):
                return flag

        pricing = item.get("pricing")
        if not isinstance(pricing, dict):
            return None
        values = [
            cls._number(pricing.get(key))
            for key in ("prompt", "completion", "input", "output", "request")
            if key in pricing
        ]
        known = [value for value in values if value is not None]
        if not known:
            return None
        if all(value == 0 for value in known):
            return True
        if any(value > 0 for value in known):
            return False
        return None

    async def list_model_infos(self) -> list[ModelInfo]:
        payload = await self._request_json("GET", "models")
        items = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(items, list):
            raise AIProviderError("AI provider returned an invalid /models response")
        models: dict[str, ModelInfo] = {}
        for item in items:
            if not isinstance(item, dict) or not item.get("id"):
                continue
            model_id = str(item["id"])
            input_modalities, output_modalities = extract_modalities(item)
            models[model_id] = ModelInfo(
                model_id=model_id,
                is_free=self._is_free_model(item),
                capabilities=extract_capabilities(item),
                input_modalities=input_modalities,
                output_modalities=output_modalities,
            )
        if not models:
            raise AIProviderError("AI provider returned no models")
        return sorted(
            models.values(),
            key=lambda model: (model.is_free is not True, model.model_id.lower()),
        )

    async def list_models(self) -> list[str]:
        return [model.model_id for model in await self.list_model_infos()]

    async def chat(self, model: str, messages: list[dict[str, Any]]) -> ChatReply:
        model = model.strip()
        if not model:
            raise AIProviderError("A model must be selected")

        cleaned: list[dict[str, Any]] = []
        for item in messages[-settings.ai_max_history_messages :]:
            content = item.get("content")
            if content in (None, "", []):
                continue
            if not isinstance(content, (str, list, dict)):
                content = str(content)
            cleaned.append(
                {
                    "role": str(item.get("role") or "user"),
                    "content": content,
                }
            )
        if not cleaned:
            raise AIProviderError("Chat message is empty")

        payload = await self._request_json(
            "POST",
            "chat/completions",
            json_body={"model": model, "messages": cleaned, "stream": False},
        )
        try:
            choice = payload["choices"][0]
            message = choice["message"]
            text = message["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise AIProviderError("AI provider returned an invalid chat response") from exc
        if isinstance(text, list):
            text = "\n".join(
                str(part.get("text") or "") if isinstance(part, dict) else str(part)
                for part in text
            )
        text = str(text or "").strip()
        if not text:
            raise AIProviderError("AI provider returned an empty response")
        response_model = str(payload.get("model") or model) if isinstance(payload, dict) else model
        return ChatReply(text=text[: settings.ai_max_reply_chars], model=response_model)

    def _error_message(self, status: int, payload: object) -> str:
        detail = ""
        if isinstance(payload, dict):
            error = payload.get("error")
            if isinstance(error, dict):
                detail = str(error.get("message") or error.get("type") or "")
            elif error:
                detail = str(error)
        detail = self._safe(detail)[:300].strip()
        return f"AI provider HTTP {status}" + (f": {detail}" if detail else "")


_provider: OpenAICompatibleProvider | None = None


def get_openai_compatible_provider() -> OpenAICompatibleProvider:
    global _provider
    if _provider is None:
        _provider = OpenAICompatibleProvider()
    return _provider
