from __future__ import annotations

import asyncio
import base64
from pathlib import Path
from urllib.parse import urlsplit

import aiohttp

from app.config import Settings
from app.errors import CancelledError
from app.security import assert_public_dns
from app.services.download_backends.base import BackendUnavailableError, DownloadBackend, DownloadRequest, NormalizedMediaResult
from app.services.media import probe_media_file


class TikTokBackend(DownloadBackend):
    """Optional adapter for an operator-controlled TikTok/Douyin sidecar API."""

    name = "tiktok"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def available(self) -> bool:
        return bool(self.settings.tiktok_backend_enabled and self.settings.tiktok_backend_url)

    async def supports(self, url: str, media_type: str | None = None) -> bool:
        host = (urlsplit(url).hostname or "").lower()
        return host.endswith(("tiktok.com", "douyin.com")) and media_type in {None, "video", "audio", "images", "story"}

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        token = self.settings.tiktok_backend_token
        if token is not None and token.get_secret_value().strip():
            headers["Authorization"] = f"Bearer {token.get_secret_value().strip()}"
        return headers

    def _payload(self, request: DownloadRequest) -> dict[str, object]:
        payload: dict[str, object] = {"url": request.url, "media_type": request.media_type}
        cookie_file = self.settings.tiktok_cookie_file
        if cookie_file is not None:
            path = cookie_file.expanduser().resolve()
            if not path.is_file():
                raise BackendUnavailableError("TIKTOK_COOKIE_FILE does not exist")
            raw = path.read_bytes()
            if len(raw) > 2 * 1024 * 1024:
                raise ValueError("TIKTOK_COOKIE_FILE exceeds the 2 MiB safety limit")
            payload["cookies_b64"] = base64.b64encode(raw).decode("ascii")
        return payload

    async def probe(self, url: str) -> NormalizedMediaResult:
        request = DownloadRequest(url=url, output_dir=Path("."), media_type="video")
        payload = await self._request(request, download=False)
        return NormalizedMediaResult(
            self.name,
            "tiktok",
            str(payload.get("media_type") or "video"),
            title=str(payload.get("title") or "TikTok media")[:500],
            duration=float(payload["duration"]) if payload.get("duration") else None,
            metadata={"item_count": len(payload.get("media") or [])},
        )

    async def _request(self, request: DownloadRequest, *, download: bool) -> dict:
        if not await self.available():
            raise BackendUnavailableError("TikTok backend is disabled or TIKTOK_BACKEND_URL is not configured")
        endpoint = f"{self.settings.tiktok_backend_url.rstrip('/')}/v1/media"
        timeout = aiohttp.ClientTimeout(total=self.settings.tiktok_backend_timeout_seconds)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(endpoint, headers=self._headers(), json=self._payload(request)) as response:
                if response.status in {401, 403}:
                    raise RuntimeError(f"TikTok sidecar HTTP {response.status}: authorization required")
                if response.status == 429:
                    raise RuntimeError("TikTok sidecar HTTP 429")
                if response.status >= 500:
                    raise RuntimeError(f"TikTok sidecar HTTP {response.status}")
                if response.status >= 400:
                    raise BackendUnavailableError(f"TikTok sidecar rejected request with HTTP {response.status}")
                payload = await response.json()
        if not isinstance(payload, dict) or not isinstance(payload.get("media"), list):
            raise RuntimeError("TikTok sidecar returned invalid response")
        return payload

    async def download(self, request: DownloadRequest) -> NormalizedMediaResult:
        payload = await self._request(request, download=True)
        request.output_dir.mkdir(parents=True, exist_ok=True)
        files: list[Path] = []
        total = 0
        try:
            for index, item in enumerate(payload["media"], 1):
                if request.cancel_event is not None and request.cancel_event.is_set():
                    raise CancelledError("TikTok sidecar download cancelled")
                if not isinstance(item, dict) or not item.get("url"):
                    continue
                media_url = assert_public_dns(str(item["url"]))
                suffix = {"image": ".jpg", "audio": ".m4a", "video": ".mp4"}.get(str(item.get("kind")), ".bin")
                output = request.output_dir / f"tiktok-{index:03d}{suffix}"
                timeout = aiohttp.ClientTimeout(total=self.settings.tiktok_backend_timeout_seconds)
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    async with session.get(media_url) as response:
                        if response.status >= 400:
                            raise RuntimeError(f"TikTok media HTTP {response.status}")
                        with output.open("wb") as handle:
                            async for chunk in response.content.iter_chunked(256 * 1024):
                                if request.cancel_event is not None and request.cancel_event.is_set():
                                    raise CancelledError("TikTok sidecar download cancelled")
                                total += len(chunk)
                                if total > self.settings.max_file_size_bytes:
                                    raise RuntimeError("TikTok outputs exceed configured download limit")
                                handle.write(chunk)
                if suffix in {".mp4", ".m4a"}:
                    await probe_media_file(output)
                elif suffix == ".jpg" and not output.read_bytes()[:3] == b"\xff\xd8\xff":
                    raise RuntimeError("TikTok sidecar returned an invalid image")
                files.append(output)
            if not files:
                raise RuntimeError("TikTok sidecar returned no downloadable media")
            return NormalizedMediaResult(
                self.name,
                "tiktok",
                str(payload.get("media_type") or request.media_type),
                title=str(payload.get("title") or "TikTok media")[:500],
                files=files,
            )
        except Exception:
            for path in files:
                path.unlink(missing_ok=True)
            for path in request.output_dir.glob("tiktok-*.*"):
                path.unlink(missing_ok=True)
            raise
