from __future__ import annotations

from typing import Any
from uuid import uuid4

import aiohttp

from app.config import get_settings
from app.security import redact_secrets
from app.services.model_capabilities import extract_runware_capabilities
from app.services.openai_compatible import AIProviderError, ModelInfo, OpenAICompatibleProvider

settings = get_settings()
_RUNWARE_CONTENT_MODELS = "https://content.runware.ai/models"


class RunwareOpenAIProvider(OpenAICompatibleProvider):
    """Runware chat client with catalog discovery and zero-cost credential probe.

    Runware's chat surface is OpenAI-compatible at /v1/chat/completions, while
    its curated model catalog is exposed by the public content service. The
    native accountManagement/getDetails operation is used only to verify that
    the configured credential is accepted; returned account data is discarded.
    """

    async def list_model_infos(self) -> list[ModelInfo]:
        timeout = aiohttp.ClientTimeout(total=settings.ai_request_timeout_seconds)
        params = {"status": "openai-compatible"}
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(_RUNWARE_CONTENT_MODELS, params=params) as response:
                    try:
                        payload = await response.json(content_type=None)
                    except (ValueError, TypeError) as exc:
                        raise AIProviderError(
                            f"Runware catalog HTTP {response.status}: invalid JSON response"
                        ) from exc
                    if response.status >= 400:
                        raise AIProviderError(f"Runware catalog HTTP {response.status}")
        except TimeoutError as exc:
            raise AIProviderError("Runware catalog request timed out") from exc
        except aiohttp.ClientError as exc:
            raise AIProviderError(f"Runware catalog network error: {type(exc).__name__}") from exc

        if isinstance(payload, dict):
            items = payload.get("items") or payload.get("data")
        else:
            items = payload
        if not isinstance(items, list):
            raise AIProviderError("Runware catalog returned an invalid model list")

        models: dict[str, ModelInfo] = {}
        for item in items:
            if not isinstance(item, dict):
                continue
            model_id = str(item.get("air") or item.get("id") or "").strip()
            if not model_id:
                continue
            capabilities, inputs, outputs = extract_runware_capabilities(item)
            models[model_id] = ModelInfo(
                model_id=model_id,
                is_free=self._runware_free_flag(item),
                capabilities=capabilities,
                input_modalities=inputs,
                output_modalities=outputs,
            )
        if not models:
            raise AIProviderError("Runware catalog returned no OpenAI-compatible models")
        return sorted(models.values(), key=lambda model: (model.is_free is not True, model.model_id.lower()))

    @staticmethod
    def _runware_free_flag(item: dict[str, Any]) -> bool | None:
        for key in ("isFree", "is_free", "free"):
            value = item.get(key)
            if isinstance(value, bool):
                return value
        overview = str(item.get("pricingOverview") or "").strip().lower()
        if overview and any(token in overview for token in ("free", "$0", "0.00")):
            return True
        return None

    async def probe_credentials(self) -> None:
        if not self.enabled:
            raise AIProviderError("Runware provider is not configured")
        timeout = aiohttp.ClientTimeout(total=settings.ai_request_timeout_seconds)
        payload = [
            {
                "taskType": "accountManagement",
                "taskUUID": str(uuid4()),
                "operation": "getDetails",
            }
        ]
        try:
            async with aiohttp.ClientSession(timeout=timeout, headers=self._headers()) as session:
                async with session.post(self.base_url, json=payload) as response:
                    try:
                        body = await response.json(content_type=None)
                    except (ValueError, TypeError) as exc:
                        raise AIProviderError(
                            f"Runware credential probe HTTP {response.status}: invalid JSON response"
                        ) from exc
                    if response.status >= 400:
                        raise AIProviderError(self._safe_probe_error(response.status, body))
        except TimeoutError as exc:
            raise AIProviderError("Runware credential probe timed out") from exc
        except aiohttp.ClientError as exc:
            raise AIProviderError(f"Runware credential probe network error: {type(exc).__name__}") from exc

        if not isinstance(body, dict) or not isinstance(body.get("data"), list) or not body["data"]:
            errors = body.get("errors") if isinstance(body, dict) else None
            if errors:
                raise AIProviderError(self._safe_probe_error(response.status, body))
            raise AIProviderError("Runware credential probe returned an invalid response")

    def _safe_probe_error(self, status: int, payload: object) -> str:
        detail = ""
        if isinstance(payload, dict):
            errors = payload.get("errors")
            if isinstance(errors, list) and errors:
                first = errors[0]
                if isinstance(first, dict):
                    detail = str(first.get("message") or first.get("code") or "")
                else:
                    detail = str(first)
            elif errors:
                detail = str(errors)
        detail = redact_secrets(detail, api_token=self.api_token)[:240].strip()
        return f"Runware credential probe HTTP {status}" + (f": {detail}" if detail else "")
