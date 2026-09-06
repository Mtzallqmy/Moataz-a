import pytest

from app.services.providers import available_qualities, detect_source, format_selector, normalize_platform
from app.services.urls import parse_bulk_urls


def test_youtube_and_facebook_profiles_remain_primary():
    assert detect_source("https://youtu.be/abc").key == "youtube"
    assert detect_source("https://www.facebook.com/reel/1").key == "facebook"


def test_generic_platform_comes_from_actual_extractor():
    assert normalize_platform("Vimeo", "https://vimeo.com/1") == "vimeo"
    assert normalize_platform("Generic", "https://example.com/video") == "generic"


def test_quality_extraction_only_returns_actual_common_heights():
    info = {
        "formats": [
            {"height": 360, "vcodec": "avc1"},
            {"height": 720, "vcodec": "avc1"},
            {"height": 1080, "vcodec": "avc1"},
            {"height": 2160, "vcodec": "av01"},
            {"height": 480, "vcodec": "none"},
        ]
    }
    assert available_qualities(info) == [360, 720, 1080, 2160]


def test_exact_resolution_selector_never_uses_less_than_or_equal():
    selector = format_selector("1080p")
    assert "height=1080" in selector
    assert "height<=" not in selector
    assert "height>=" not in selector


def test_audio_and_best_selectors():
    assert format_selector("audio") == "bestaudio/best"
    assert format_selector("best") == "bestvideo+bestaudio/best"
    with pytest.raises(ValueError):
        format_selector("99")


def test_bulk_url_dedup_normalizes_tracking_and_creates_independent_inputs():
    text = """
    https://example.com/v/1?utm_source=x
    https://example.com/v/1
    https://youtu.be/a
    https://youtu.be/a#frag
    """
    result = parse_bulk_urls(text)
    assert result.urls == ("https://example.com/v/1?utm_source=x", "https://youtu.be/a")
    assert result.duplicates == 2


def test_bulk_limit_is_enforced():
    text = "\n".join(f"https://example.com/{i}" for i in range(20))
    assert len(parse_bulk_urls(text, limit=4).urls) == 4
