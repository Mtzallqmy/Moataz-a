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
    "youtube",
    "YouTube",
    frozenset({"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be"}),
    ("youtube",),
)
FACEBOOK = SourceProfile(
    "facebook",
    "Facebook",
    frozenset({"facebook.com", "www.facebook.com", "m.facebook.com", "web.facebook.com", "fb.watch"}),
    ("facebook",),
)
SUPPORTED_SOURCES = (YOUTUBE, FACEBOOK)
COMMON_HEIGHTS = (360, 480, 720, 1080, 1440, 2160)


def _host_matches(host: str, allowed: str) -> bool:
    return host == allowed or host.endswith(f".{allowed}")


def detect_source(url: str) -> SourceProfile | None:
    try:
        host = (urlparse(url).hostname or "").lower().rstrip(".")
    except ValueError:
        return None
    for profile in SUPPORTED_SOURCES:
        if any(_host_matches(host, allowed) for allowed in profile.hosts):
            return profile
    return None


def normalize_platform(extractor_key: str | None, url: str) -> str:
    extractor = (extractor_key or "").lower().strip()
    for profile in SUPPORTED_SOURCES:
        if any(extractor.startswith(prefix) for prefix in profile.extractor_prefixes):
            return profile.key
    known = detect_source(url)
    if known:
        return known.key
    # Generic support is based on the extractor that yt-dlp actually selected.
    if extractor and extractor not in {"generic", "unsupported"}:
        return extractor.split(":", 1)[0][:32]
    return "generic"


def available_qualities(info: dict[str, Any]) -> list[int]:
    heights = {
        int(fmt["height"])
        for fmt in info.get("formats") or []
        if fmt.get("height")
        and fmt.get("vcodec") not in {None, "none"}
        and int(fmt["height"]) > 0
    }
    common = [height for height in COMMON_HEIGHTS if height in heights]
    return common if common else sorted(heights)[-8:]


def format_selector(quality: str) -> str:
    value = quality.strip().lower()
    if value in {"audio", "mp3"}:
        return "bestaudio/best"
    if value == "best":
        return "bestvideo+bestaudio/best"
    height = int(value.removesuffix("p"))
    if height < 144 or height > 4320:
        raise ValueError("Unsupported video height")
    # Exact height only: never silently downgrade a user-selected resolution.
    return (
        f"bestvideo[height={height}][ext=mp4]+bestaudio[ext=m4a]/"
        f"bestvideo[height={height}]+bestaudio/"
        f"best[height={height}]"
    )
