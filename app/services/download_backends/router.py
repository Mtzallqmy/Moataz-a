from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass
from urllib.parse import urlsplit

from app.errors import ErrorCode, classify_error
from app.security import redact_secrets
from app.services.download_backends.base import (
    BackendUnavailableError,
    DownloadBackend,
    DownloadRequest,
    NormalizedMediaResult,
)
from app.services.download_backends.health import BackendHealthRegistry

logger = logging.getLogger("moataz.downloader.router")


@dataclass(frozen=True, slots=True)
class DetectedMedia:
    platform: str
    media_type: str


class PlatformDetector:
    @staticmethod
    def detect(url: str, media_type: str | None = None) -> DetectedMedia:
        parsed = urlsplit(url)
        host = (parsed.hostname or "").lower().removeprefix("www.")
        path = parsed.path.lower()
        if host in {"youtu.be", "youtube.com", "m.youtube.com", "music.youtube.com"}:
            platform = "youtube"
        elif host.endswith("instagram.com"):
            platform = "instagram"
        elif host.endswith(("tiktok.com", "douyin.com")):
            platform = "tiktok"
        elif host.endswith(("facebook.com", "fb.watch")):
            platform = "facebook"
        elif host.endswith("snapchat.com"):
            platform = "snapchat"
        elif host.endswith(("twitter.com", "x.com")):
            platform = "twitter"
        elif host.endswith("vimeo.com"):
            platform = "vimeo"
        elif host.endswith("reddit.com"):
            platform = "reddit"
        else:
            platform = "generic"

        kind = media_type or "video"
        if platform == "instagram" and "/stories/" in path:
            kind = "story"
        elif platform == "instagram" and "/highlights/" in path:
            kind = "highlight"
        elif platform == "tiktok" and "/story/" in path:
            kind = "story"
        elif any(value in path for value in ("/p/", "/photo/", "/photos/", "/pin/")):
            kind = "images"
        return DetectedMedia(platform, kind)


DEFAULT_ROUTES: dict[tuple[str, str], tuple[str, ...]] = {
    ("youtube", "*"): ("yt-dlp", "cobalt", "pytubefix"),
    ("instagram", "story"): ("gallery-dl", "instaloader", "yt-dlp", "cobalt"),
    ("instagram", "highlight"): ("gallery-dl", "instaloader", "yt-dlp", "cobalt"),
    ("instagram", "*"): ("yt-dlp", "cobalt", "gallery-dl", "instaloader"),
    ("tiktok", "story"): ("gallery-dl", "tiktok", "yt-dlp", "cobalt"),
    ("tiktok", "*"): ("yt-dlp", "cobalt", "tiktok", "gallery-dl"),
    ("facebook", "*"): ("yt-dlp", "cobalt", "gallery-dl"),
    ("generic", "*"): ("yt-dlp", "cobalt", "gallery-dl"),
}

_FALLBACK_ERRORS = {
    ErrorCode.ANTI_BOT,
    ErrorCode.UNSUPPORTED_EXTRACTOR,
    ErrorCode.FORMAT_UNAVAILABLE,
    ErrorCode.EXTRACTOR_ERROR,
    ErrorCode.HTTP_403,
    ErrorCode.HTTP_429,
    ErrorCode.NETWORK_TIMEOUT,
    ErrorCode.UPSTREAM_5XX,
    ErrorCode.BACKEND_UNAVAILABLE,
}


class ProviderRouter:
    def __init__(
        self,
        backends: Iterable[DownloadBackend],
        *,
        health: BackendHealthRegistry | None = None,
    ) -> None:
        self.backends = {backend.name: backend for backend in backends}
        self.health = health or BackendHealthRegistry()

    def order(self, detected: DetectedMedia) -> tuple[DownloadBackend, ...]:
        names = DEFAULT_ROUTES.get(
            (detected.platform, detected.media_type),
            DEFAULT_ROUTES.get((detected.platform, "*"), DEFAULT_ROUTES[("generic", "*")]),
        )
        return tuple(self.backends[name] for name in names if name in self.backends)

    @staticmethod
    def allows_fallback(exc: BaseException) -> bool:
        return classify_error(exc).code in _FALLBACK_ERRORS


class DownloadManager:
    def __init__(
        self,
        router: ProviderRouter,
        *,
        detector: PlatformDetector | None = None,
    ) -> None:
        self.router = router
        self.detector = detector or PlatformDetector()
        self.last_attempts: tuple[dict[str, str], ...] = ()

    async def _candidates(self, url: str, media_type: str | None):
        detected = self.detector.detect(url, media_type)
        for backend in self.router.order(detected):
            if not self.router.health.allows(backend.name, detected.platform):
                continue
            if not await backend.available():
                continue
            if await backend.supports(url, detected.media_type):
                yield detected, backend

    async def probe(self, url: str, media_type: str | None = None) -> NormalizedMediaResult:
        return await self._execute(url, media_type, None)

    async def expand_playlist(self, url: str, *, limit: int | None = None) -> list[dict[str, object]]:
        attempts: list[dict[str, str]] = []
        last_error: BaseException | None = None
        had_candidate = False
        async for detected, backend in self._candidates(url, "playlist"):
            had_candidate = True
            try:
                entries = await backend.expand_playlist(url, limit=limit)
                self.router.health.success(backend.name, detected.platform)
                attempts.append({"backend": backend.name, "result": "SUCCESS"})
                self.last_attempts = tuple(attempts)
                logger.info(
                    "job=- platform=%s backend=%s result=SUCCESS fallback_count=%s",
                    detected.platform,
                    backend.name,
                    len(attempts) - 1,
                )
                return entries
            except Exception as exc:
                last_error = exc
                error = classify_error(exc)
                attempts.append({"backend": backend.name, "result": error.code.value})
                self.router.health.failure(backend.name, detected.platform, error)
                if not self.router.allows_fallback(exc):
                    break
        self.last_attempts = tuple(attempts)
        if last_error is not None:
            raise last_error
        if not had_candidate:
            raise BackendUnavailableError("No configured download backend supports playlists for this URL")
        raise BackendUnavailableError("All configured playlist backends failed")

    async def download(self, request: DownloadRequest) -> NormalizedMediaResult:
        return await self._execute(request.url, request.media_type, request)

    async def _execute(
        self,
        url: str,
        media_type: str | None,
        request: DownloadRequest | None,
    ) -> NormalizedMediaResult:
        attempts: list[dict[str, str]] = []
        last_error: BaseException | None = None
        had_candidate = False
        job_key = request.job_key if request and request.job_key else "-"
        async for detected, backend in self._candidates(url, media_type):
            had_candidate = True
            try:
                result = await (backend.download(request) if request else backend.probe(url))
                self.router.health.success(backend.name, detected.platform)
                attempts.append({"backend": backend.name, "result": "SUCCESS"})
                self.last_attempts = tuple(attempts)
                logger.info(
                    "job=%s platform=%s backend=%s result=SUCCESS fallback_count=%s",
                    job_key,
                    detected.platform,
                    backend.name,
                    len(attempts) - 1,
                )
                result.provider = backend.name
                result.platform = detected.platform
                result.metadata.setdefault("attempted_backends", attempts.copy())
                result.metadata.setdefault("successful_backend", backend.name)
                result.metadata.setdefault("normalized_error", None)
                result.metadata.setdefault("fallback_count", len(attempts) - 1)
                return result
            except Exception as exc:
                last_error = exc
                error = classify_error(exc)
                attempts.append({"backend": backend.name, "result": error.code.value})
                self.router.health.failure(backend.name, detected.platform, error)
                logger.warning(
                    "job=%s platform=%s backend=%s result=%s fallback_count=%s fallback=%s detail=%s",
                    job_key,
                    detected.platform,
                    backend.name,
                    error.code.value,
                    len(attempts) - 1,
                    self.router.allows_fallback(exc),
                    redact_secrets(exc)[:1000],
                )
                if not self.router.allows_fallback(exc):
                    break
        self.last_attempts = tuple(attempts)
        if last_error is not None:
            raise last_error
        if not had_candidate:
            raise BackendUnavailableError("No configured download backend supports this URL")
        raise BackendUnavailableError("All configured download backends failed")

    async def healthcheck(self, url: str) -> dict[str, str]:
        detected = self.detector.detect(url)
        result: dict[str, str] = {}
        for backend in self.router.order(detected):
            available = await backend.available()
            result[backend.name] = self.router.health.status(
                backend.name, detected.platform, available=available
            ).value
        return result
