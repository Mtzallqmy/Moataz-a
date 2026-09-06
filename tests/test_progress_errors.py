import pytest

from app.errors import ErrorCode, classify_error, retry_delay
from app.progress import ProgressSnapshot


def test_progress_calculation_and_rendering():
    snapshot = ProgressSnapshot.from_ytdlp(
        {"downloaded_bytes": 25, "total_bytes": 100, "speed": 2048, "eta": 12}
    )
    assert snapshot.percent == 25.0
    text = snapshot.render(job_id=7, status="DOWNLOADING", quality="720")
    for value in ("Job #7", "Status: DOWNLOADING", "Quality: 720", "25.0%", "2.0 KB/s", "00:12"):
        assert value in text


@pytest.mark.parametrize(
    ("message", "code", "retryable"),
    [
        ("invalid URL", ErrorCode.INVALID_URL, False),
        ("Private video", ErrorCode.PRIVATE_MEDIA, False),
        ("Login required", ErrorCode.AUTH_REQUIRED, False),
        ("Requested format is not available", ErrorCode.FORMAT_UNAVAILABLE, False),
        ("request timed out", ErrorCode.NETWORK_TIMEOUT, True),
        ("HTTP Error 429", ErrorCode.HTTP_429, True),
        ("HTTP Error 502", ErrorCode.UPSTREAM_5XX, True),
        ("Telegram network timeout", ErrorCode.TELEGRAM_NETWORK, True),
        ("File too large", ErrorCode.FILE_TOO_LARGE, False),
        ("FFmpeg failed", ErrorCode.FFMPEG_ERROR, False),
    ],
)
def test_retry_classification(message, code, retryable):
    result = classify_error(RuntimeError(message))
    assert result.code is code
    assert result.retryable is retryable


def test_exponential_backoff_has_cap_and_deterministic_jitter():
    assert retry_delay(0, base=4, cap=45, jitter_ratio=0, random_value=0.5) == 4
    assert retry_delay(3, base=4, cap=45, jitter_ratio=0, random_value=0.5) == 32
    assert retry_delay(10, base=4, cap=45, jitter_ratio=0, random_value=0.5) == 45
