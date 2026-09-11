from __future__ import annotations

import logging
from pathlib import Path

import pytest

from app.config import Settings
from app.errors import ErrorCode, classify_error
from app.security import redact_secrets
from app.services.download_backends import (
    BackendHealthRegistry,
    DownloadBackend,
    DownloadManager,
    DownloadRequest,
    HealthStatus,
    NormalizedMediaResult,
    PlatformDetector,
    ProviderRouter,
)
from app.services.downloader import DownloaderService


class ScriptedBackend(DownloadBackend):
    def __init__(
        self,
        name: str,
        *,
        error: Exception | None = None,
        suffix: str = ".mp4",
        available: bool = True,
        playlist: list[dict[str, object]] | None = None,
    ) -> None:
        self.name = name
        self.error = error
        self.suffix = suffix
        self.available_flag = available
        self.playlist = playlist
        self.calls = 0

    async def available(self) -> bool:
        return self.available_flag

    async def supports(self, url: str, media_type: str | None = None) -> bool:  # noqa: ARG002
        return True

    async def probe(self, url: str) -> NormalizedMediaResult:  # noqa: ARG002
        self.calls += 1
        if self.error:
            raise self.error
        return NormalizedMediaResult(self.name, "generic", "video", title=self.name)

    async def download(self, request: DownloadRequest) -> NormalizedMediaResult:
        self.calls += 1
        if self.error:
            raise self.error
        return NormalizedMediaResult(
            self.name,
            "generic",
            request.media_type,
            files=[request.output_dir / f"{self.name}{self.suffix}"],
        )

    async def expand_playlist(
        self, url: str, *, limit: int | None = None  # noqa: ARG002
    ) -> list[dict[str, object]]:
        self.calls += 1
        if self.error:
            raise self.error
        if self.playlist is None:
            return await super().expand_playlist(url, limit=limit)
        return self.playlist[:limit] if limit is not None else self.playlist


def manager(*backends: DownloadBackend, health: BackendHealthRegistry | None = None) -> DownloadManager:
    return DownloadManager(
        ProviderRouter(backends, health=health, register_optional=False)
    )


@pytest.mark.asyncio
async def test_youtube_ytdlp_cobalt_pytubefix_fallback(tmp_path: Path):
    yt = ScriptedBackend("yt-dlp", error=RuntimeError("Sign in to confirm you’re not a bot"))
    cobalt = ScriptedBackend("cobalt", error=RuntimeError("HTTP Error 503: Service Unavailable"))
    pytube = ScriptedBackend("pytubefix")
    download_manager = manager(yt, cobalt, pytube)

    result = await download_manager.download(
        DownloadRequest(
            "https://youtube.com/watch?v=abc",
            tmp_path,
            job_key="job-1",
        )
    )

    assert result.provider == "pytubefix"
    assert [yt.calls, cobalt.calls, pytube.calls] == [1, 1, 1]
    assert result.metadata["fallback_count"] == 2
    assert download_manager.route_telemetry("job-1") == {
        "attempted_backends": ["yt-dlp", "cobalt", "pytubefix"],
        "successful_backend": "pytubefix",
        "normalized_error": None,
        "fallback_count": 2,
    }


@pytest.mark.asyncio
async def test_all_youtube_backends_fail_with_last_error_and_telemetry(tmp_path: Path):
    yt = ScriptedBackend("yt-dlp", error=RuntimeError("HTTP Error 403: Forbidden"))
    cobalt = ScriptedBackend("cobalt", error=RuntimeError("HTTP 429 Too Many Requests"))
    pytube = ScriptedBackend("pytubefix", error=TimeoutError("socket timed out"))
    download_manager = manager(yt, cobalt, pytube)

    with pytest.raises(TimeoutError, match="socket timed out"):
        await download_manager.download(
            DownloadRequest(
                "https://youtube.com/watch?v=abc",
                tmp_path,
                job_key="job-2",
            )
        )

    telemetry = download_manager.route_telemetry("job-2")
    assert telemetry["attempted_backends"] == ["yt-dlp", "cobalt", "pytubefix"]
    assert telemetry["successful_backend"] is None
    assert telemetry["normalized_error"] == ErrorCode.NETWORK_TIMEOUT.value
    assert telemetry["fallback_count"] == 2


@pytest.mark.parametrize(
    "message",
    [
        "HTTP Error 403: Forbidden",
        "HTTP 429 Too Many Requests",
        "Sign in to confirm you’re not a bot",
        "temporary extractor failure: unable to extract",
    ],
)
@pytest.mark.asyncio
async def test_retryable_route_errors_fallback(message: str, tmp_path: Path):
    primary = ScriptedBackend("yt-dlp", error=RuntimeError(message))
    fallback = ScriptedBackend("cobalt")
    download_manager = manager(primary, fallback)

    result = await download_manager.download(
        DownloadRequest("https://youtube.com/watch?v=x", tmp_path)
    )

    assert result.provider == "cobalt"
    assert primary.calls == fallback.calls == 1


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("Login required; use --cookies", ErrorCode.AUTH_REQUIRED),
        ("This video is private", ErrorCode.PRIVATE_MEDIA),
    ],
)
@pytest.mark.asyncio
async def test_protected_media_never_falls_back(message: str, expected: ErrorCode):
    primary = ScriptedBackend("yt-dlp", error=RuntimeError(message))
    fallback = ScriptedBackend("cobalt")
    download_manager = manager(primary, fallback)

    with pytest.raises(RuntimeError, match=message.split(";")[0]):
        await download_manager.probe("https://youtube.com/watch?v=private")

    assert classify_error(primary.error).code is expected
    assert primary.calls == 1
    assert fallback.calls == 0


@pytest.mark.asyncio
async def test_private_media_does_not_open_platform_circuit():
    health = BackendHealthRegistry(failure_threshold=1, cooldown_seconds=60)
    primary = ScriptedBackend("yt-dlp", error=RuntimeError("This video is private"))
    download_manager = manager(primary, health=health)

    with pytest.raises(RuntimeError, match="private"):
        await download_manager.probe("https://youtube.com/watch?v=private")

    assert health.status("yt-dlp", "youtube") is HealthStatus.HEALTHY
    assert health.allows("yt-dlp", "youtube")


@pytest.mark.asyncio
async def test_circuit_breaker_is_per_platform_and_recovers_after_cooldown():
    now = [10.0]
    health = BackendHealthRegistry(
        failure_threshold=1,
        cooldown_seconds=5,
        clock=lambda: now[0],
    )
    error = classify_error(RuntimeError("HTTP Error 503"))
    health.failure("yt-dlp", "youtube", error)

    assert health.status("yt-dlp", "youtube") is HealthStatus.OPEN
    assert health.status("yt-dlp", "instagram") is HealthStatus.HEALTHY
    now[0] = 16.0
    assert health.allows("yt-dlp", "youtube")
    assert health.status("yt-dlp", "youtube") is HealthStatus.DEGRADED


def test_platform_routing_covers_specialist_content_types():
    detector = PlatformDetector()
    backends = [
        ScriptedBackend(name)
        for name in ("yt-dlp", "cobalt", "pytubefix", "gallery-dl", "instaloader", "tiktok")
    ]
    router = ProviderRouter(backends, register_optional=False)

    assert detector.detect("https://www.instagram.com/stories/user/123/").media_type == "story"
    assert (
        detector.detect("https://www.instagram.com/stories/highlights/456/").media_type
        == "highlight"
    )
    assert detector.detect("https://www.instagram.com/p/carousel/").media_type == "images"
    assert detector.detect("https://www.tiktok.com/@u/photo/1").media_type == "images"

    assert [b.name for b in router.order(detector.detect("https://youtube.com/watch?v=1"))] == [
        "yt-dlp",
        "cobalt",
        "pytubefix",
    ]
    assert [
        b.name
        for b in router.order(detector.detect("https://instagram.com/stories/user/1/"))
    ] == ["gallery-dl", "instaloader", "yt-dlp", "cobalt"]
    assert [
        b.name
        for b in router.order(
            detector.detect("https://instagram.com/stories/highlights/1/")
        )
    ] == ["gallery-dl", "instaloader", "yt-dlp", "cobalt"]
    assert [
        b.name
        for b in router.order(detector.detect("https://tiktok.com/@u/video/1", "audio"))
    ] == ["yt-dlp", "cobalt", "tiktok", "gallery-dl"]
    assert [
        b.name for b in router.order(detector.detect("https://facebook.com/watch/?v=1"))
    ] == ["yt-dlp", "cobalt", "gallery-dl"]


@pytest.mark.asyncio
async def test_playlist_falls_through_to_backend_with_playlist_support():
    yt = ScriptedBackend("yt-dlp", error=RuntimeError("Unable to extract data"))
    cobalt = ScriptedBackend("cobalt")
    pytube = ScriptedBackend(
        "pytubefix",
        playlist=[
            {"url": "https://youtu.be/1", "title": "one", "index": 1},
            {"url": "https://youtu.be/2", "title": "two", "index": 2},
        ],
    )
    download_manager = manager(yt, cobalt, pytube)

    entries = await download_manager.expand_playlist(
        "https://youtube.com/playlist?list=abc",
        limit=2,
    )

    assert len(entries) == 2
    assert pytube.calls == 1


def test_facade_mp3_uses_download_manager_after_fallback(tmp_path: Path):
    primary = ScriptedBackend("yt-dlp", error=RuntimeError("HTTP Error 403"))
    cobalt = ScriptedBackend("cobalt", suffix=".mp3")
    download_manager = manager(primary, cobalt)
    service = DownloaderService(
        Settings(render_temp_dir=tmp_path / "tmp"),
        url_guard=lambda url: url,
        manager=download_manager,
    )

    output = service.download_audio(
        "https://youtube.com/watch?v=audio",
        tmp_path,
        job_key="mp3-job",
    )

    assert output.name == "cobalt.mp3"
    assert download_manager.route_telemetry("mp3-job")["successful_backend"] == "cobalt"


def test_redaction_covers_backend_credentials_and_signed_urls(caplog):
    secret_text = (
        "Cookie: session=secret; Authorization: Bearer bearer-secret; "
        "Api-Key: cobalt-secret; https://proxy-user:proxy-pass@proxy.example/ "
        "https://cdn.example/file?X-Amz-Signature=signed-secret&token=api-secret"
    )
    safe = redact_secrets(secret_text)

    for forbidden in (
        "session=secret",
        "bearer-secret",
        "cobalt-secret",
        "proxy-user",
        "proxy-pass",
        "signed-secret",
        "api-secret",
    ):
        assert forbidden not in safe

    with caplog.at_level(logging.WARNING):
        logging.getLogger("moataz.downloader.router").warning("detail=%s", safe)
    assert "signed-secret" not in caplog.text
