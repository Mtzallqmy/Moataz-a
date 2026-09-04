from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import yt_dlp

from app.config import get_settings

ProgressHook = Callable[[dict[str, Any]], None]
settings = get_settings()


@dataclass(slots=True)
class MediaInfo:
    title: str
    duration: int | None
    thumbnail: str | None
    platform: str
    qualities: list[int]


def _base_options() -> dict[str, Any]:
    options: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "socket_timeout": 30,
        "retries": 3,
        "fragment_retries": 3,
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

    qualities = sorted(
        {
            int(fmt["height"])
            for fmt in info.get("formats") or []
            if fmt.get("height") and fmt.get("vcodec") not in {None, "none"}
        }
    )
    common = [q for q in (360, 480, 720, 1080, 1440, 2160) if q in qualities]
    if not common and qualities:
        common = qualities[-6:]

    return MediaInfo(
        title=(info.get("title") or "Untitled")[:500],
        duration=int(info["duration"]) if info.get("duration") else None,
        thumbnail=info.get("thumbnail"),
        platform=(info.get("extractor_key") or info.get("extractor") or "unknown")[:32],
        qualities=common,
    )


def _format_selector(quality: str) -> str:
    if quality == "audio":
        return "bestaudio/best"
    if quality == "best":
        return "bestvideo+bestaudio/best"
    height = int(quality)
    return (
        f"bestvideo[height<={height}]+bestaudio/"
        f"best[height<={height}]/bestvideo+bestaudio/best"
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
            "format": _format_selector(quality),
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

    candidates = [
        path
        for path in job_dir.iterdir()
        if path.is_file()
        and path.suffix.lower() in {".mp4", ".mkv", ".webm", ".mp3", ".m4a"}
        and not path.name.endswith(".part")
    ]
    if not candidates:
        raise RuntimeError("yt-dlp finished without a media output file")
    return max(candidates, key=lambda path: path.stat().st_size)
