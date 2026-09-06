from __future__ import annotations

import ipaddress
import socket
from collections.abc import Callable
from urllib.parse import SplitResult, parse_qsl, urlencode, urlsplit, urlunsplit

from app.errors import InvalidURLError

Resolver = Callable[..., list[tuple]]
_BLOCKED_NAMES = {"localhost", "localhost.localdomain", "metadata.google.internal"}
_TRACKING_PARAMS = {"utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "fbclid", "gclid"}


def _is_public_ip(value: str) -> bool:
    ip = ipaddress.ip_address(value)
    return not (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def normalize_media_url(url: str) -> str:
    raw = str(url or "").strip()
    if not raw or len(raw) > 4096:
        raise InvalidURLError("Invalid URL")
    try:
        parsed = urlsplit(raw)
    except ValueError as exc:
        raise InvalidURLError("Invalid URL") from exc
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise InvalidURLError("Only HTTP/HTTPS URLs are allowed")
    if parsed.username or parsed.password:
        raise InvalidURLError("Credential-bearing URLs are not allowed")

    host = parsed.hostname.lower().rstrip(".")
    if host in _BLOCKED_NAMES or host.endswith(".localhost"):
        raise InvalidURLError("Unsafe URL host")
    try:
        if not _is_public_ip(host):
            raise InvalidURLError("Private or special IP addresses are not allowed")
    except ValueError:
        pass

    try:
        port = parsed.port
    except ValueError as exc:
        raise InvalidURLError("Invalid URL port") from exc
    if port is not None and port not in {80, 443}:
        raise InvalidURLError("Only standard HTTP/HTTPS ports are allowed")

    netloc = host if port is None else f"{host}:{port}"
    if ":" in host and not host.startswith("["):
        netloc = f"[{host}]" if port is None else f"[{host}]:{port}"
    clean = SplitResult(parsed.scheme.lower(), netloc, parsed.path or "/", parsed.query, "")
    return urlunsplit(clean)


def canonicalize_url(url: str) -> str:
    normalized = normalize_media_url(url)
    parsed = urlsplit(normalized)
    query = [(k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True) if k.lower() not in _TRACKING_PARAMS]
    query.sort()
    path = parsed.path.rstrip("/") or "/"
    return urlunsplit((parsed.scheme, parsed.netloc, path, urlencode(query, doseq=True), ""))


def validate_media_url(url: str) -> bool:
    try:
        normalize_media_url(url)
        return True
    except InvalidURLError:
        return False


def assert_public_dns(url: str, *, resolver: Resolver = socket.getaddrinfo) -> str:
    normalized = normalize_media_url(url)
    host = urlsplit(normalized).hostname
    if not host:
        raise InvalidURLError("Invalid URL")
    try:
        ipaddress.ip_address(host)
        return normalized
    except ValueError:
        pass

    try:
        records = resolver(host, None, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise InvalidURLError("URL host could not be resolved") from exc
    addresses = {record[4][0].split("%")[0] for record in records if record and len(record) >= 5}
    if not addresses:
        raise InvalidURLError("URL host could not be resolved")
    if any(not _is_public_ip(address) for address in addresses):
        raise InvalidURLError("SSRF protection blocked a private or special address")
    return normalized


def redact_secrets(value: object, *, bot_token: str = "", database_url: str = "") -> str:
    text = str(value)
    for secret in (bot_token, database_url):
        if secret and len(secret) >= 6:
            text = text.replace(secret, "[REDACTED]")
    # Telegram tokens have a stable numeric-prefix:secret shape; redact even if not configured here.
    import re

    text = re.sub(r"\b\d{6,12}:[A-Za-z0-9_-]{20,}\b", "[REDACTED_TELEGRAM_TOKEN]", text)
    text = re.sub(r"(?i)(postgres(?:ql)?(?:\+asyncpg)?://)[^\s@]+@", r"\1[REDACTED]@", text)
    return text
