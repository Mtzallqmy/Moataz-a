from app.jobs import classify_job_error


def test_network_errors_are_retryable():
    info = classify_job_error(RuntimeError("HTTP Error 503: Service Unavailable"))
    assert info.code == "NETWORK"
    assert info.retryable is True


def test_timeout_errors_are_retryable():
    info = classify_job_error(TimeoutError("request timed out"))
    assert info.code == "NETWORK"
    assert info.retryable is True


def test_private_media_is_not_retryable():
    info = classify_job_error(RuntimeError("Private video. Sign in to confirm your age"))
    assert info.code == "ACCESS_REQUIRED"
    assert info.retryable is False


def test_file_size_failure_is_not_retryable():
    info = classify_job_error(RuntimeError("File exceeds the official upload limit"))
    assert info.code == "FILE_TOO_LARGE"
    assert info.retryable is False


def test_unknown_failures_are_not_retried_by_default():
    info = classify_job_error(RuntimeError("unexpected state"))
    assert info.code == "UNKNOWN"
    assert info.retryable is False
