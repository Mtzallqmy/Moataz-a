from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path

from app.config import Settings
from app.errors import CancelledError, FormatUnavailableError
from app.services.download_backends.base import (
    BackendUnavailableError,
    DownloadBackend,
    DownloadRequest,
    NormalizedMediaResult,
)
from app.services.media import _run_process, probe_media_file


class PytubefixBackend(DownloadBackend):
    name = "pytubefix"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def available(self) -> bool:
        return bool(self.settings.pytubefix_enabled and importlib.util.find_spec("pytubefix"))

    async def supports(self, url: str, media_type: str | None = None) -> bool:  # noqa: ARG002
        return "youtu" in url.lower() and media_type in {None, "video", "audio", "playlist"}

    @staticmethod
    def _progressive_key(stream) -> tuple[int, int, str]:
        resolution = str(getattr(stream, "resolution", "") or "").removesuffix("p")
        return (
            int(resolution) if resolution.isdigit() else 0,
            int(getattr(stream, "fps", 0) or 0),
            str(getattr(stream, "itag", "")),
        )

    @staticmethod
    def _audio_key(stream) -> tuple[int, str]:
        abr = str(getattr(stream, "abr", "") or "").removesuffix("kbps")
        return (int(abr) if abr.isdigit() else 0, str(getattr(stream, "itag", "")))

    async def probe(self, url: str) -> NormalizedMediaResult:
        if not await self.available():
            raise BackendUnavailableError("pytubefix optional dependency is unavailable")

        def run() -> NormalizedMediaResult:
            from pytubefix import YouTube

            yt = YouTube(url)
            yt.check_availability()
            formats = []
            for stream in yt.streams.filter(type="video"):
                resolution = str(getattr(stream, "resolution", "") or "")
                height = (
                    int(resolution.removesuffix("p"))
                    if resolution.removesuffix("p").isdigit()
                    else None
                )
                if height:
                    formats.append(
                        {
                            "format_id": str(stream.itag),
                            "height": height,
                            "ext": getattr(stream, "subtype", None),
                            "fps": getattr(stream, "fps", None),
                        }
                    )
            return NormalizedMediaResult(
                provider=self.name,
                platform="youtube",
                media_type="video",
                title=yt.title,
                thumbnail=yt.thumbnail_url,
                duration=float(getattr(yt, "length", 0) or 0) or None,
                formats=formats,
                metadata={"qualities": sorted({item["height"] for item in formats})},
            )

        return await asyncio.to_thread(run)

    async def expand_playlist(self, url: str, *, limit: int | None = None) -> list[dict[str, object]]:
        if not await self.available():
            raise BackendUnavailableError("pytubefix optional dependency is unavailable")

        def run() -> list[dict[str, object]]:
            from pytubefix import Playlist

            playlist = Playlist(url)
            urls = list(playlist.video_urls)
            safe_limit = (
                self.settings.max_playlist_items
                if limit is None
                else min(limit, self.settings.max_playlist_items)
            )
            if len(urls) > safe_limit:
                raise ValueError(f"Playlist exceeds safe limit of {safe_limit} items")
            return [
                {"url": item, "title": None, "index": index}
                for index, item in enumerate(urls, 1)
            ]

        return await asyncio.to_thread(run)

    async def download(self, request: DownloadRequest) -> NormalizedMediaResult:
        if not await self.available():
            raise BackendUnavailableError("pytubefix optional dependency is unavailable")
        request.output_dir.mkdir(parents=True, exist_ok=True)
        cancel_event = request.cancel_event

        def interrupted() -> bool:
            return bool(cancel_event and cancel_event.is_set())

        def download_streams() -> tuple[Path, Path | None]:
            from pytubefix import YouTube

            yt = YouTube(request.url)
            yt.check_availability()
            if request.media_type == "audio":
                audio = sorted(
                    yt.streams.filter(only_audio=True), key=self._audio_key, reverse=True
                )
                if not audio:
                    raise FormatUnavailableError("pytubefix found no audio stream")
                path = Path(
                    audio[0].download(
                        output_path=str(request.output_dir),
                        filename="pytubefix-audio.m4a",
                        timeout=self.settings.ytdlp_socket_timeout_seconds,
                        max_retries=self.settings.ytdlp_retries,
                        interrupt_checker=interrupted,
                    )
                )
                return path, None

            requested = request.quality.lower().strip().removesuffix("p")
            progressive = sorted(
                yt.streams.filter(progressive=True, file_extension="mp4"),
                key=self._progressive_key,
                reverse=True,
            )
            if requested != "best":
                progressive = [
                    s
                    for s in progressive
                    if str(getattr(s, "resolution", "")).removesuffix("p") == requested
                ]
            if progressive:
                path = Path(
                    progressive[0].download(
                        output_path=str(request.output_dir),
                        filename="pytubefix-video.mp4",
                        timeout=self.settings.ytdlp_socket_timeout_seconds,
                        max_retries=self.settings.ytdlp_retries,
                        interrupt_checker=interrupted,
                    )
                )
                return path, None

            adaptive = sorted(
                yt.streams.filter(only_video=True, file_extension="mp4"),
                key=self._progressive_key,
                reverse=True,
            )
            if requested != "best":
                adaptive = [
                    s
                    for s in adaptive
                    if str(getattr(s, "resolution", "")).removesuffix("p") == requested
                ]
            audio = sorted(
                yt.streams.filter(only_audio=True), key=self._audio_key, reverse=True
            )
            if not adaptive or not audio:
                raise FormatUnavailableError(
                    "pytubefix found no deterministic stream combination"
                )
            video_path = Path(
                adaptive[0].download(
                    output_path=str(request.output_dir),
                    filename="pytubefix-video-only.mp4",
                    timeout=self.settings.ytdlp_socket_timeout_seconds,
                    max_retries=self.settings.ytdlp_retries,
                    interrupt_checker=interrupted,
                )
            )
            audio_path = Path(
                audio[0].download(
                    output_path=str(request.output_dir),
                    filename="pytubefix-audio-only.m4a",
                    timeout=self.settings.ytdlp_socket_timeout_seconds,
                    max_retries=self.settings.ytdlp_retries,
                    interrupt_checker=interrupted,
                )
            )
            return video_path, audio_path

        video_or_audio, secondary = await asyncio.to_thread(download_streams)
        if interrupted():
            raise CancelledError("pytubefix download cancelled")
        if secondary is not None:
            output = request.output_dir / "pytubefix-merged.mp4"
            await _run_process(
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                str(video_or_audio),
                "-i",
                str(secondary),
                "-c:v",
                "copy",
                "-c:a",
                "aac",
                "-movflags",
                "+faststart",
                str(output),
                timeout=self.settings.ffmpeg_timeout_seconds,
                cancel_event=cancel_event,
            )
            video_or_audio.unlink(missing_ok=True)
            secondary.unlink(missing_ok=True)
            final = output
        elif request.media_type == "audio":
            output = request.output_dir / "pytubefix-audio.mp3"
            await _run_process(
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                str(video_or_audio),
                "-vn",
                "-c:a",
                "libmp3lame",
                "-b:a",
                "192k",
                str(output),
                timeout=self.settings.ffmpeg_timeout_seconds,
                cancel_event=cancel_event,
            )
            video_or_audio.unlink(missing_ok=True)
            final = output
        else:
            final = video_or_audio
        if final.stat().st_size > self.settings.max_file_size_bytes:
            final.unlink(missing_ok=True)
            raise RuntimeError("pytubefix output exceeds configured download limit")
        await probe_media_file(final)
        return NormalizedMediaResult(self.name, "youtube", request.media_type, files=[final])
