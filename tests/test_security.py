import socket

import pytest

from app.errors import InvalidURLError
from app.security import (
    assert_public_dns,
    canonicalize_url,
    normalize_media_url,
    redact_secrets,
    validate_media_url,
)


def _resolver(address: str):
    def resolve(host, port, *, type):  # noqa: A002, ARG001
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 0))]

    return resolve


def test_url_validation_accepts_public_http_https_shapes():
    assert validate_media_url("https://www.youtube.com/watch?v=abc")
    assert validate_media_url("https://example.com/media/1")
    assert validate_media_url("http://public.example/video")


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "ftp://example.com/file",
        "http://localhost/a",
        "http://127.0.0.1/a",
        "http://10.0.0.1/a",
        "http://172.16.0.1/a",
        "http://192.168.1.1/a",
        "http://169.254.169.254/latest/meta-data",
        "http://[::1]/a",
        "https://user:pass@example.com/a",
        "https://example.com:8080/a",
    ],
)
def test_url_validation_blocks_ssrf_and_unsafe_schemes(url):
    assert not validate_media_url(url)


def test_dns_ssrf_rejects_private_resolution():
    with pytest.raises(InvalidURLError):
        assert_public_dns("https://example.com/x", resolver=_resolver("10.0.0.7"))


def test_dns_ssrf_accepts_public_resolution():
    assert assert_public_dns("https://example.com/x", resolver=_resolver("93.184.216.34")) == "https://example.com/x"


def test_dns_ssrf_rejects_mixed_public_and_private_answers():
    def mixed(host, port, *, type):  # noqa: A002, ARG001
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 0)),
        ]

    with pytest.raises(InvalidURLError):
        assert_public_dns("https://example.com/x", resolver=mixed)


def test_url_normalization_and_dedup_remove_tracking_and_fragments():
    first = canonicalize_url("HTTPS://Example.com/video/?b=2&utm_source=x&a=1#part")
    second = canonicalize_url("https://example.com/video?a=1&b=2")
    assert first == second == "https://example.com/video?a=1&b=2"


def test_malformed_port_is_invalid_not_an_unhandled_value_error():
    with pytest.raises(InvalidURLError):
        normalize_media_url("https://example.com:notaport/video")


def test_secret_redaction_covers_configured_and_pattern_secrets():
    token = "123456:ABCDEFGHIJKLMNOPQRSTUVWXYZ1234567890"
    db = "postgresql+asyncpg://alice:secret@db.example/app"
    text = redact_secrets(f"token={token} db={db}", bot_token=token, database_url=db)
    assert token not in text
    assert "alice:secret" not in text
    assert "REDACTED" in text
