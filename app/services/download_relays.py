from __future__ import annotations

import logging
import re
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit

import requests

from app.config import Settings
from app.errors import CancelledError
from app.security import assert_public_dns, redact_secrets

logger = logging.getLogger("moataz.downloader.relay")
_SAFE_EXTENSIONS = {".mp4", ".webm", ".mkv", ".mp3", ".m4a", ".ogg", ".opus", ".wav"}


@dataclass(frozen=True, slots=True)
class RelayProbe:
    title: str
    platform: str
    endpoint_index: int


def _secret_values(value: Any) -> str:
    if value is None:
        return ""
    getter = getattr(value, "get_secret_value", None)
    return str(getter() if getter else value).strip()


def _configured_urls(value: Any) -> tuple[str, ...]:
    raw = _secret_values(value)
    result: list[str] = []
    for candidate in re.split(r"[,\n]", raw):
        url = candidate.strip().rstrip("/")
        if not url or url in result:
            continue
        parsed = urlsplit(url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.fragment:
            raise ValueError("Download relay URLs must be public HTTPS API roots")
        result.append(url)
    if len(result) > 4:
        raise ValueError("At most four download relay URLs may be configured")
    return tuple(result)


class CobaltRelayClient:
    """Bounded fallback client for authorized or self-hosted Cobalt instances."""

    def __init__(
        self,
        settings: Settings,
        *,
        session_factory: Callable[[], requests.Session] = requests.Session,
        url_guard: Callable[[str], str] = assert_public_dns,
    ) -> None:
        self.settings = settings
        self.endpoints = _configured_urls(settings.cobalt_api_urls)
        self.session_factory = session_factory
        self.url_guard = url_guard

    @property
    def enabled(self) -> bool:
        return bool(self.endpoints)

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        token = _secret_values(self.settings.cobalt_api_token)
        if token:
            headers["Authorization"] = f"{self.settings.cobalt_auth_scheme} {token}"
        return headers

    @staticmethod
    def _request_body(url: str, quality: str) -> dict[str, Any]:
        normalized = quality.lower().strip().removesuffix("p")
        audio = normalized in {"audio", "mp3"}
        body: dict[str, Any] = {
            "url": url,
            "downloadMode": "audio" if audio else "auto",
            "filenameStyle": "basic",
            "alwaysProxy": True,
            "disableMetadata": False,
        }
        if audio:
            body.update({"audioFormat": "mp3", "audioBitrate": "256"})
        else:
            body.update(
                {
                    "videoQuality": "max" if normalized == "best" else normalized,
                    "youtubeVideoCodec": "h264",
                    "youtubeVideoContainer": "mp4",
                }
            )
        return body

    def _ticket(self, endpoint: str, url: str, quality: str) -> dict[str, Any]:
        with self.session_factory() as session:
            response = session.post(
                endpoint + "/",
                json=self._request_body(url, quality),
                headers=self._headers(),
                timeout=(10, self.settings.cobalt_timeout_seconds),
            )
            if response.status_code == 429:
                raise RuntimeError("HTTP 429 from download relay")
            if response.status_code in {401, 403}:
                raise RuntimeError("Download relay authentication required")
            if response.status_code >= 500:
                raise RuntimeError(f"Download relay upstream HTTP {response.status_code}")
            response.raise_for_status()
            try:
                payload = response.json()
            except ValueError as exc:
                raise RuntimeError("Download relay returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise RuntimeError("Download relay returned invalid data")
        if payload.get("status") == "error":
            code = str((payload.get("error") or {}).get("code") or "unknown")
            if "unsupported" in code:
                raise RuntimeError("Unsupported URL at download relay")
            if "auth" in code:
                raise RuntimeError("Download relay authentication required")
            raise RuntimeError(f"Temporary extractor failure at download relay: {code[:160]}")
        if payload.get("status") not in {"tunnel", "redirect"}:
            raise RuntimeError(f"Unsupported download relay response: {payload.get('status')}")
        target = str(payload.get("url") or "").strip()
        if not target:
            raise RuntimeError("Download relay response has no media URL")
        payload["url"] = urljoin(endpoint + "/", target)
        payload["_relay_endpoint"] = endpoint
        return payload

    def _attempt(self, operation: str, callback: Callable[[str, int], Any]) -> Any:
        last: Exception | None = None
        for index, endpoint in enumerate(self.endpoints, start=1):
            try:
                return callback(endpoint, index)
            except Exception as exc:
                last = exc
                logger.warning(
                    "download relay %s failed endpoint=%s error_type=%s detail=%s",
                    operation,
                    index,
                    type(exc).__name__,
                    redact_secrets(exc, api_token=_secret_values(self.settings.cobalt_api_token))[:1000],
                )
        if last is None:
            raise RuntimeError("No download relay is configured")
        raise last

    def probe(self, url: str) -> RelayProbe:
        def run(endpoint: str, index: int) -> RelayProbe:
            payload = self._ticket(endpoint, url, "best")
            filename = Path(str(payload.get("filename") or "download")).name
            host = (urlsplit(url).hostname or "media").removeprefix("www.")
            return RelayProbe(
                title=Path(filename).stem[:500] or "Media",
                platform=host.split(".")[0] or "relay",
                endpoint_index=index,
            )

        return self._attempt("probe", run)

    def download(
        self,
        url: str,
        quality: str,
        job_dir: Path,
        *,
        cancel_event: threading.Event,
        progress_hook: Callable[[dict[str, Any]], None] | None = None,
    ) -> Path:
        def run(endpoint: str, index: int) -> Path:
            payload = self._ticket(endpoint, url, quality)
            return self._stream(
                payload,
                job_dir,
                quality=quality,
                cancel_event=cancel_event,
                progress_hook=progress_hook,
                endpoint_index=index,
            )

        return self._attempt("download", run)

    def _stream(
        self,
        ticket: dict[str, Any],
        job_dir: Path,
        *,
        quality: str,
        cancel_event: threading.Event,
        progress_hook: Callable[[dict[str, Any]], None] | None,
        endpoint_index: int,
    ) -> Path:
        target = self.url_guard(str(ticket["url"]))
        relay_host = urlsplit(str(ticket.get("_relay_endpoint") or "")).hostname
        target_host = urlsplit(target).hostname
        download_headers = {"Accept": "*/*"}
        if relay_host and target_host == relay_host:
            download_headers.update(self._headers())
        audio = quality.lower().strip().removesuffix("p") in {"audio", "mp3"}
        remote_name = Path(str(ticket.get("filename") or "")).name
        suffix = Path(remote_name).suffix.lower()
        if audio:
            suffix = ".mp3"
        elif suffix not in _SAFE_EXTENSIONS:
            suffix = ".mp4"
        output = job_dir / f"relay-{endpoint_index}-{uuid.uuid4().hex[:12]}{suffix}"
        started = time.monotonic()
        downloaded = 0
        job_dir.mkdir(parents=True, exist_ok=True)
        try:
            with self.session_factory() as session, session.get(
                target,
                stream=True,
                allow_redirects=False,
                timeout=(10, self.settings.cobalt_timeout_seconds),
                headers=download_headers,
            ) as response:
                if 300 <= response.status_code < 400:
                    raise RuntimeError("Download relay returned an unchecked redirect")
                response.raise_for_status()
                total = int(response.headers.get("Content-Length") or 0)
                estimated = int(response.headers.get("Estimated-Content-Length") or 0)
                expected = total or estimated or None
                if total > self.settings.max_file_size_bytes:
                    raise RuntimeError("File too large for configured download limit")
                with output.open("xb") as stream:
                    for chunk in response.iter_content(chunk_size=256 * 1024):
                        if cancel_event.is_set():
                            raise CancelledError("Download cancelled")
                        if not chunk:
                            continue
                        downloaded += len(chunk)
                        if downloaded > self.settings.max_file_size_bytes:
                            raise RuntimeError("File too large for configured download limit")
                        stream.write(chunk)
                        if progress_hook:
                            progress_hook(
                                {
                                    "status": "downloading",
                                    "downloaded_bytes": downloaded,
                                    "total_bytes": expected,
                                    "elapsed": time.monotonic() - started,
                                }
                            )
            if downloaded <= 0:
                raise RuntimeError("Download relay produced an empty file")
            return output
        except Exception:
            output.unlink(missing_ok=True)
            raise
