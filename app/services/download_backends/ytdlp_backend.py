from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from app.services.download_backends.base import DownloadBackend, DownloadRequest, NormalizedMediaResult

if TYPE_CHECKING:
    from app.services.downloader import _YtDlpEngine


class YtDlpBackend(DownloadBackend):
    name = "yt-dlp"

    def __init__(self, engine: _YtDlpEngine) -> None:
        self.engine = engine

    async def probe(self, url: str) -> NormalizedMediaResult:
        info = await asyncio.to_thread(self.engine.probe, url)
        formats = [
            {
                "format_id": item.format_id,
                "height": item.height,
                "ext": item.ext,
                "fps": item.fps,
                "tbr": item.tbr,
            }
            for item in info.formats
        ]
        return NormalizedMediaResult(
            provider=self.name,
            platform=info.platform,
            media_type="playlist" if info.is_playlist else "video",
            title=info.title,
            duration=float(info.duration) if info.duration is not None else None,
            formats=formats,
            thumbnail=info.thumbnail,
            metadata={
                "uploader": info.uploader,
                "qualities": list(info.qualities),
                "webpage_url": info.webpage_url,
                "media_id": info.media_id,
                "extractor": info.extractor,
                "is_playlist": info.is_playlist,
                "playlist_count": info.playlist_count,
            },
        )

    async def expand_playlist(self, url: str, *, limit: int | None = None) -> list[dict[str, object]]:
        entries = await asyncio.to_thread(self.engine.expand_playlist, url, limit=limit)
        return [{"url": item.url, "title": item.title, "index": item.index} for item in entries]

    async def download(self, request: DownloadRequest) -> NormalizedMediaResult:
        output = await asyncio.to_thread(
            self.engine.download,
            request.url,
            request.quality,
            request.output_dir,
            cancel_event=request.cancel_event,
            progress_hook=request.progress_hook,
            known_qualities=list(request.known_qualities) if request.known_qualities else None,
        )
        return NormalizedMediaResult(
            provider=self.name,
            platform="generic",
            media_type=request.media_type,
            files=[output],
            metadata={"job_key": request.job_key},
        )
