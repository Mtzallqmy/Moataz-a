from app.services.providers import (
    available_qualities,
    detect_source,
    format_selector,
    normalize_platform,
)


def test_detect_supported_sources():
    assert detect_source("https://youtu.be/abc").key == "youtube"
    assert detect_source("https://www.youtube.com/watch?v=abc").key == "youtube"
    assert detect_source("https://www.facebook.com/reel/123").key == "facebook"
    assert detect_source("https://fb.watch/abc").key == "facebook"
    assert detect_source("https://example.com/video") is None


def test_normalize_platform_prefers_extractor_then_url():
    assert normalize_platform("Youtube", "https://example.com") == "youtube"
    assert normalize_platform("FacebookPluginsVideo", "https://example.com") == "facebook"
    assert normalize_platform(None, "https://youtu.be/abc") == "youtube"
    assert normalize_platform(None, "https://example.com") == "unknown"


def test_available_qualities_prefers_common_heights():
    info = {
        "formats": [
            {"height": 240, "vcodec": "avc1"},
            {"height": 360, "vcodec": "avc1"},
            {"height": 720, "vcodec": "avc1"},
            {"height": 1080, "vcodec": "avc1"},
            {"height": None, "vcodec": "none"},
        ]
    }
    assert available_qualities(info) == [360, 720, 1080]


def test_format_selector_is_telegram_friendly_and_bounded():
    assert "ext=mp4" in format_selector("720")
    assert "ext=m4a" in format_selector("720")
    assert format_selector("audio").startswith("bestaudio")
    assert "bv*" in format_selector("best")
