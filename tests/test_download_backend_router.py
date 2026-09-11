from __future__ import annotations

from pathlib import Path

import pytest

from app.errors import ErrorCode, classify_error
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


class FakeBackend(DownloadBackend):
    def __init__(self, name: str, *, result=None, error: Exception | None = None):
        self.name = name
        self.result = result
        self.error = error
        self.calls = 0

    async def supports(self, url, media_type=None):  # noqa: ARG002
        return True

    async def probe(self, url):  # noqa: ARG002
        self.calls += 1
        if self.error:
            raise self.error
        return self.result or NormalizedMediaResult(self.name, "generic", "video")

    async def download(self, request):
        self.calls += 1
        if self.error:
            raise self.error
        return self.result or NormalizedMediaResult(
            self.name,
            "generic",
            request.media_type,
            files=[request.output_dir / "media.mp4"],
        )


def test_platform_detector_distinguishes_specialized_content():
    detector = PlatformDetector()
    assert detector.detect("https://youtu.be/id").platform == "youtube"
    assert detector.detect("https://instagram.com/stories/user/1").media_type == "story"
    assert detector.detect("https://instagram.com/highlights/1").media_type == "highlight"
    assert detector.detect("https://tiktok.com/@u/photo/1").media_type == "images"


@pytest.mark.asyncio
async def test_router_uses_platform_order_and_hides_earlier_failure(tmp_path: Path):
    primary = FakeBackend("yt-dlp", error=RuntimeError("Unable to extract data"))
    cobalt = FakeBackend(
        "cobalt",
        result=NormalizedMediaResult(
            "cobalt", "youtube", "video", files=[tmp_path / "fallback.mp4"]
        ),
    )
    third = FakeBackend("pytubefix")
    manager = DownloadManager(ProviderRouter([third, cobalt, primary]))
    result = await manager.download(
        DownloadRequest("https://youtube.com/watch?v=x", tmp_path)
    )
    assert result.provider == "cobalt"
    assert primary.calls == cobalt.calls == 1
    assert third.calls == 0
    assert result.metadata["fallback_count"] == 1
    assert manager.last_attempts == (
        {"backend": "yt-dlp", "result": "EXTRACTOR_ERROR"},
        {"backend": "cobalt", "result": "SUCCESS"},
    )


@pytest.mark.asyncio
async def test_auth_required_does_not_fallback():
    primary = FakeBackend("yt-dlp", error=RuntimeError("Login required; use --cookies"))
    cobalt = FakeBackend("cobalt")
    manager = DownloadManager(ProviderRouter([primary, cobalt]))
    with pytest.raises(RuntimeError, match="Login required"):
        await manager.probe("https://youtube.com/watch?v=private")
    assert primary.calls == 1
    assert cobalt.calls == 0


def test_circuit_breaker_is_platform_scoped_and_ignores_auth_errors():
    now = [100.0]
    health = BackendHealthRegistry(
        failure_threshold=2,
        cooldown_seconds=10,
        clock=lambda: now[0],
    )
    auth = classify_error(RuntimeError("Login required; use --cookies"))
    temporary = classify_error(RuntimeError("HTTP Error 503"))
    health.failure("yt-dlp", "youtube", auth)
    assert health.allows("yt-dlp", "youtube")
    health.failure("yt-dlp", "youtube", temporary)
    health.failure("yt-dlp", "youtube", temporary)
    assert health.status("yt-dlp", "youtube") is HealthStatus.OPEN
    assert health.status("yt-dlp", "instagram") is HealthStatus.HEALTHY
    now[0] += 11
    assert health.allows("yt-dlp", "youtube")
    assert health.status("yt-dlp", "youtube") is HealthStatus.DEGRADED


@pytest.mark.parametrize(
    ("message", "code"),
    [
        ("HTTP Error 403: Forbidden", ErrorCode.HTTP_403),
        ("No configured download backend supports this URL", ErrorCode.BACKEND_UNAVAILABLE),
    ],
)
def test_new_backend_errors_are_classified(message, code):
    if code is ErrorCode.BACKEND_UNAVAILABLE:
        from app.services.download_backends import BackendUnavailableError

        error = BackendUnavailableError(message)
    else:
        error = RuntimeError(message)
    assert classify_error(error).code is code
