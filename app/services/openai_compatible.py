from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import aiohttp

from app.config import get_settings
from app.security import redact_secrets

settings = get_settings()


class AIProviderError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ChatReply:
    text: str
    model: str


class OpenAICompatibleProvider:
    """Minimal OpenAI-compatible client without an SDK dependency.

    OPENAI_BASE_URL is treated as the API root, normally ending in `/v1`.
    Only `/models` and `/chat/completions` are used.
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
        json_body: dict[str, Any] | None = None,
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

    async def list_models(self) -> list[str]:
        payload = await self._request_json("GET", "models")
        items = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(items, list):
            raise AIProviderError("AI provider returned an invalid /models response")
        models = sorted({str(item.get("id")) for item in items if isinstance(item, dict) and item.get("id")})
        if not models:
            raise AIProviderError("AI provider returned no models")
        return models

    async def chat(self, model: str, messages: list[dict[str, str]]) -> ChatReply:
        model = model.strip()
        if not model:
            raise AIProviderError("A model must be selected")
        cleaned = [
            {"role": str(item.get("role") or "user"), "content": str(item.get("content") or "")}
            for item in messages[-settings.ai_max_history_messages :]
            if item.get("content")
        ]
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
