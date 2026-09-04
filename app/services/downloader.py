from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yt_dlp

from app.config import get_settings
from app.services.providers import available_qualities, format_selector, normalize_platform

ProgressHook = Callable[[dict[str, Any]], None]
settings = get_settings()


@dataclass(slots=True)
class MediaInfo:
    title: str
    duration: int | None
    thumbnail: str | None
    platform: str
    qualities: list[int]
    media_id: str | None = None
    uploader: str | None = None
    webpage_url: str | None = None


def _base_options() -> dict[str, Any]:
    options: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "socket_timeout": settings.ytdlp_socket_timeout_seconds,
        "retries": settings.ytdlp_retries,
        "fragment_retries": settings.ytdlp_fragment_retries,
        "extractor_retries": settings.ytdlp_retries,
        "concurrent_fragment_downloads": settings.ytdlp_concurrent_fragments,
        "cachedir": False,
    }
    if settings.ytdlp_cookies_file:
        options["cookiefile"] = settings.ytdlp_cookies_file
    return options


def _single_info(info: dict[str, Any]) -> dict[str, Any]:
    if info.get("_type") in {"playlist", "multi_video"}:
        entries = [entry for entry in info.get("entries") or [] if entry]
        if not entries:
            raise RuntimeError("No downloadable media found")
        return entries[0]
    return info


def probe_media(url: str) -> MediaInfo:
    opts = _base_options()
    opts["skip_download"] = True
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = _single_info(ydl.extract_info(url, download=False))

    extractor_key = info.get("extractor_key") or info.get("extractor")
    return MediaInfo(
        title=(info.get("title") or "Untitled")[:500],
        duration=int(info["duration"]) if info.get("duration") else None,
        thumbnail=info.get("thumbnail"),
        platform=normalize_platform(extractor_key, url),
        qualities=available_qualities(info),
        media_id=str(info.get("id"))[:128] if info.get("id") else None,
        uploader=(info.get("uploader") or info.get("channel") or None),
        webpage_url=info.get("webpage_url") or url,
    )


def download_media(
    url: str,
    quality: str,
    job_dir: Path,
    progress_hook: ProgressHook | None = None,
) -> Path:
    job_dir.mkdir(parents=True, exist_ok=True)
    opts = _base_options()
    opts.update(
        {
            "format": format_selector(quality),
            "outtmpl": str(job_dir / "%(id)s.%(ext)s"),
            "merge_output_format": "mp4",
            "prefer_ffmpeg": True,
            "continuedl": True,
            "overwrites": False,
        }
    )
    if progress_hook:
        opts["progress_hooks"] = [progress_hook]

    if quality == "audio":
        opts["postprocessors"] = [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }
        ]

    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.download([url])

    allowed_suffixes = {".mp3", ".m4a"} if quality == "audio" else {".mp4", ".mkv", ".webm"}
    candidates = [
        path
        for path in job_dir.iterdir()
        if path.is_file()
        and path.suffix.lower() in allowed_suffixes
        and not path.name.endswith((".part", ".ytdl"))
    ]
    if not candidates:
        raise RuntimeError("yt-dlp finished without a media output file")
    return max(candidates, key=lambda path: path.stat().st_size)
