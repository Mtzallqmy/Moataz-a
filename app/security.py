import ipaddress
from urllib.parse import urlparse

ALLOWED_HOSTS = {
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "music.youtube.com",
    "youtu.be",
    "facebook.com",
    "www.facebook.com",
    "m.facebook.com",
    "web.facebook.com",
    "fb.watch",
}


def validate_media_url(url: str) -> bool:
    try:
        parsed = urlparse(url.strip())
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return False
        host = parsed.hostname.lower().rstrip(".")
        try:
            ipaddress.ip_address(host)
            return False
        except ValueError:
            pass
        return host in ALLOWED_HOSTS or any(
            host.endswith(f".{allowed}") for allowed in ALLOWED_HOSTS
        )
    except Exception:
        return False
