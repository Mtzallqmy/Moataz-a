from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.errors import ErrorCode, MediaError

ProgressHook = Callable[[dict[str, Any]], None]


class BackendUnavailableError(MediaError):
    code = ErrorCode.BACKEND_UNAVAILABLE
    retryable = True


@dataclass(frozen=True, slots=True)
class DownloadRequest:
    url: str
    output_dir: Path
    media_type: str = "video"
    quality: str = "best"
    job_key: str = ""
    cancel_event: threading.Event | None = None
    progress_hook: ProgressHook | None = None
    known_qualities: tuple[int, ...] | None = None


@dataclass(slots=True)
class NormalizedMediaResult:
    provider: str
    platform: str
    media_type: str
    title: str = "Media"
    duration: float | None = None
    formats: list[dict[str, Any]] = field(default_factory=list)
    files: list[Path] = field(default_factory=list)
    thumbnail: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def primary_file(self) -> Path:
        if not self.files:
            raise RuntimeError(f"{self.provider} returned no media files")
        return self.files[0]


class DownloadBackend:
    """Provider-neutral async backend contract.

    Optional integrations must report unavailable instead of failing module import.
    Implementations never receive arbitrary output paths from a Telegram user.
    """

    name = "backend"

    async def available(self) -> bool:
        return True

    async def supports(self, url: str, media_type: str | None = None) -> bool:
        return True

    async def probe(self, url: str) -> NormalizedMediaResult:
        raise BackendUnavailableError(f"{self.name} does not support probing")

    async def list_formats(self, url: str) -> list[dict[str, Any]]:
        return (await self.probe(url)).formats

    async def expand_playlist(self, url: str, *, limit: int | None = None) -> list[dict[str, Any]]:
        raise BackendUnavailableError(f"{self.name} does not support playlists")

    async def download(self, request: DownloadRequest) -> NormalizedMediaResult:
        raise BackendUnavailableError(f"{self.name} does not support downloads")

    async def download_video(
        self, url: str, quality: str, output_dir: Path, **kwargs: Any
    ) -> NormalizedMediaResult:
        return await self.download(
            DownloadRequest(url=url, quality=quality, output_dir=output_dir, **kwargs)
        )

    async def download_audio(
        self, url: str, format: str, output_dir: Path, **kwargs: Any  # noqa: A002
    ) -> NormalizedMediaResult:
        return await self.download(
            DownloadRequest(
                url=url,
                quality=format,
                media_type="audio",
                output_dir=output_dir,
                **kwargs,
            )
        )

    async def download_images(
        self, url: str, output_dir: Path, **kwargs: Any
    ) -> NormalizedMediaResult:
        return await self.download(
            DownloadRequest(url=url, media_type="images", output_dir=output_dir, **kwargs)
        )

    async def download_story(
        self, url: str, output_dir: Path, **kwargs: Any
    ) -> NormalizedMediaResult:
        return await self.download(
            DownloadRequest(url=url, media_type="story", output_dir=output_dir, **kwargs)
        )

    async def healthcheck(self) -> bool:
        return await self.available()
