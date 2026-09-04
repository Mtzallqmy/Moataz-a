import pytest

from app.config import Settings
from app.security import validate_media_url
from app.utils import parse_time, progress_bar, seconds_to_hms


def test_parse_time_variants():
    assert parse_time("15") == 15
    assert parse_time("01:30") == 90
    assert parse_time("01:02:03") == 3723


def test_parse_time_invalid():
    with pytest.raises(ValueError):
        parse_time("00:99:00")
    with pytest.raises(ValueError):
        parse_time("-5")


def test_progress_bar():
    assert progress_bar(0, 4) == "░░░░"
    assert progress_bar(50, 4) == "██░░"
    assert progress_bar(100, 4) == "████"


def test_seconds_to_hms():
    assert seconds_to_hms(90) == "0:01:30"


def test_media_url_allowlist():
    assert validate_media_url("https://www.youtube.com/watch?v=abc")
    assert validate_media_url("https://youtu.be/abc")
    assert validate_media_url("https://www.facebook.com/reel/123")
    assert validate_media_url("https://fb.watch/abc")


def test_media_url_rejects_ssrf_and_unknown_hosts():
    assert not validate_media_url("http://127.0.0.1:8000/secret")
    assert not validate_media_url("http://192.168.1.1/")
    assert not validate_media_url("file:///etc/passwd")
    assert not validate_media_url("https://evil.example/youtube.com")


def test_stale_local_telegram_api_setting_is_ignored():
    settings = Settings(_env_file=None, telegram_local_api_url="Moatax ai")
    assert not hasattr(settings, "telegram_local_api_url")


def test_invalid_app_mode_falls_back_to_polling():
    settings = Settings(_env_file=None, app_mode="broken-value")
    assert settings.app_mode == "polling"
