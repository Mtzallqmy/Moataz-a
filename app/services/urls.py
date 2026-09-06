from __future__ import annotations

import re
from dataclasses import dataclass

from app.config import get_settings
from app.security import canonicalize_url, normalize_media_url

settings = get_settings()
_URL_RE = re.compile(r"https?://[^\s,<>]+", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class BulkParseResult:
    urls: tuple[str, ...]
    rejected: int
    duplicates: int


def parse_bulk_urls(text: str, *, limit: int | None = None) -> BulkParseResult:
    max_items = settings.max_bulk_urls if limit is None else max(1, int(limit))
    seen: set[str] = set()
    urls: list[str] = []
    rejected = 0
    duplicates = 0
    for raw in _URL_RE.findall(text or ""):
        candidate = raw.rstrip(".);]}،")
        try:
            normalized = normalize_media_url(candidate)
            key = canonicalize_url(normalized)
        except Exception:
            rejected += 1
            continue
        if key in seen:
            duplicates += 1
            continue
        seen.add(key)
        urls.append(normalized)
        if len(urls) >= max_items:
            break
    return BulkParseResult(tuple(urls), rejected, duplicates)
