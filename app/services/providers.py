from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse


@dataclass(frozen=True, slots=True)
class SourceProfile:
    key: str
    display_name: str
    hosts: frozenset[str]
    extractor_prefixes: tuple[str, ...]


YOUTUBE = SourceProfile(
    key="youtube",
    display_name="YouTube",
    hosts=frozenset({"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be"}),
    extractor_prefixes=("youtube",),
)

FACEBOOK = SourceProfile(
    key="facebook",
    display_name="Facebook",
    hosts=frozenset(
        {
            "facebook.com",
            "www.facebook.com",
            "m.facebook.com",
            "web.facebook.com",
            "fb.watch",
        }
    ),
    extractor_prefixes=("facebook",),
)

SUPPORTED_SOURCES = (YOUTUBE, FACEBOOK)


def _host_matches(host: str, allowed: str) -> bool:
    return host == allowed or host.endswith(f".{allowed}")


def detect_source(url: str) -> SourceProfile | None:
    """Return the supported source profile for a URL without performing network I/O."""

    try:
        host = (urlparse(url.strip()).hostname or "").lower().rstrip(".")
    except Exception:
        return None
    if not host:
        return None
    for profile in SUPPORTED_SOURCES:
        if any(_host_matches(host, allowed) for allowed in profile.hosts):
            return profile
    return None


def normalize_platform(extractor_key: str | None, url: str) -> str:
    """Normalize yt-dlp extractor names into stable product-facing platform keys."""

    extractor = (extractor_key or "").lower().strip()
    for profile in SUPPORTED_SOURCES:
        if any(extractor.startswith(prefix) for prefix in profile.extractor_prefixes):
            return profile.key
    detected = detect_source(url)
    return detected.key if detected else "unknown"


def available_qualities(info: dict[str, Any]) -> list[int]:
    """Return a compact, stable list of video heights exposed to Telegram users."""

    heights = sorted(
        {
            int(fmt["height"])
            for fmt in info.get("formats") or []
            if fmt.get("height")
            and fmt.get("vcodec") not in {None, "none"}
            and int(fmt["height"]) > 0
        }
    )
    common = [height for height in (360, 480, 720, 1080, 1440, 2160) if height in heights]
    if common:
        return common
    return heights[-6:]


def format_selector(quality: str) -> str:
    """Prefer Telegram-friendly MP4/M4A while retaining broad yt-dlp fallbacks."""

    value = quality.strip().lower()
    if value == "audio":
        return "bestaudio[ext=m4a]/bestaudio/best"
    if value == "best":
        return "bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]/bv*+ba/b"

    height = int(value)
    if height < 144 or height > 4320:
        raise ValueError("Unsupported video height")
    return (
        f"bv*[height<={height}][ext=mp4]+ba[ext=m4a]/"
        f"b[height<={height}][ext=mp4]/"
        f"bv*[height<={height}]+ba/b[height<={height}]"
    )
