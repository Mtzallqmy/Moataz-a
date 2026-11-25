"""Signed public artifact links, shared by server and Android-host profiles."""
from __future__ import annotations

import hashlib
import hmac
import os
import re
import time
from urllib.parse import urlsplit

from app.config import Settings


def _base_url(settings: Settings) -> str | None:
    raw = settings.artifact_public_base_url.strip() or os.getenv("RAILWAY_PUBLIC_DOMAIN", "").strip()
    if not raw:
        return None
    if "://" not in raw:
        raw = f"https://{raw}"
    try:
        parsed = urlsplit(raw)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.path not in {"", "/"} or parsed.query or parsed.fragment or parsed.port not in {None, 443}:
            return None
    except ValueError:
        return None
    return raw.rstrip("/")


def _signature(settings: Settings, job_id: int, expires: int) -> str:
    key = hashlib.sha256(b"moataz-artifact-delivery-v1\0" + settings.bot_token.encode()).digest()
    return hmac.new(key, f"{job_id}:{expires}".encode(), hashlib.sha256).hexdigest()


def artifact_download_link(settings: Settings, job_id: int, *, now: int | None = None) -> str | None:
    base = _base_url(settings)
    if not base or not settings.bot_token:
        return None
    expires = (int(time.time()) if now is None else now) + min(settings.artifact_link_ttl_seconds, settings.temp_retention_seconds)
    return f"{base}/files/artifacts/{job_id}?expires={expires}&token={_signature(settings, job_id, expires)}"


def valid_artifact_token(settings: Settings, job_id: int, expires: int, token: str, *, now: int | None = None) -> bool:
    current = int(time.time()) if now is None else now
    return bool(settings.bot_token and job_id > 0 and current <= expires <= current + settings.artifact_link_ttl_seconds and re.fullmatch(r"[0-9a-f]{64}", token) is not None and hmac.compare_digest(_signature(settings, job_id, expires), token))
