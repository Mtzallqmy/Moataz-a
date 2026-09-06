from __future__ import annotations

import random
from dataclasses import dataclass
from enum import StrEnum


class ErrorCode(StrEnum):
    INVALID_URL = "INVALID_URL"
    MEDIA_UNAVAILABLE = "MEDIA_UNAVAILABLE"
    PRIVATE_MEDIA = "PRIVATE_MEDIA"
    AUTH_REQUIRED = "AUTH_REQUIRED"
    FORMAT_UNAVAILABLE = "FORMAT_UNAVAILABLE"
    EXTRACTOR_ERROR = "EXTRACTOR_ERROR"
    NETWORK_TIMEOUT = "NETWORK_TIMEOUT"
    HTTP_429 = "HTTP_429"
    UPSTREAM_5XX = "UPSTREAM_5XX"
    TELEGRAM_NETWORK = "TELEGRAM_NETWORK"
    TELEGRAM_UPLOAD = "TELEGRAM_UPLOAD"
    FILE_TOO_LARGE = "FILE_TOO_LARGE"
    FFMPEG_ERROR = "FFMPEG_ERROR"
    DATABASE_ERROR = "DATABASE_ERROR"
    STORAGE_ERROR = "STORAGE_ERROR"
    CANCELLED = "CANCELLED"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class ErrorInfo:
    code: ErrorCode
    retryable: bool


class MediaError(RuntimeError):
    code = ErrorCode.UNKNOWN
    retryable = False


class InvalidURLError(MediaError):
    code = ErrorCode.INVALID_URL


class FormatUnavailableError(MediaError):
    code = ErrorCode.FORMAT_UNAVAILABLE


class CancelledError(MediaError):
    code = ErrorCode.CANCELLED


class FFmpegError(MediaError):
    code = ErrorCode.FFMPEG_ERROR


def classify_error(exc: BaseException) -> ErrorInfo:
    if isinstance(exc, MediaError):
        return ErrorInfo(exc.code, exc.retryable)

    name = type(exc).__name__.lower()
    text = f"{name} {str(exc).lower()}"

    if "cancel" in text:
        return ErrorInfo(ErrorCode.CANCELLED, False)
    if "requested format" in text or "format is not available" in text:
        return ErrorInfo(ErrorCode.FORMAT_UNAVAILABLE, False)
    if "private" in text:
        return ErrorInfo(ErrorCode.PRIVATE_MEDIA, False)
    if any(marker in text for marker in ("login required", "sign in", "authentication", "cookies")):
        return ErrorInfo(ErrorCode.AUTH_REQUIRED, False)
    if any(marker in text for marker in ("unsupported url", "invalid url", "unsafe url", "ssrf")):
        return ErrorInfo(ErrorCode.INVALID_URL, False)
    if any(marker in text for marker in ("video unavailable", "media unavailable", "has been removed", "not available")):
        return ErrorInfo(ErrorCode.MEDIA_UNAVAILABLE, False)
    if "ffmpeg" in text or "ffprobe" in text:
        return ErrorInfo(ErrorCode.FFMPEG_ERROR, False)
    if any(marker in text for marker in ("file too large", "file exceeds", "max_file_size")):
        return ErrorInfo(ErrorCode.FILE_TOO_LARGE, False)
    if "telegram" in text and any(marker in text for marker in ("timeout", "network", "connection")):
        return ErrorInfo(ErrorCode.TELEGRAM_NETWORK, True)
    if "telegram" in text and any(marker in text for marker in ("upload", "entity too large", "bad request")):
        return ErrorInfo(ErrorCode.TELEGRAM_UPLOAD, False)
    if "429" in text or "too many requests" in text:
        return ErrorInfo(ErrorCode.HTTP_429, True)
    if any(marker in text for marker in ("500", "502", "503", "504", "service unavailable", "bad gateway")):
        return ErrorInfo(ErrorCode.UPSTREAM_5XX, True)
    if any(marker in text for marker in ("timeout", "timed out", "socket timeout")):
        return ErrorInfo(ErrorCode.NETWORK_TIMEOUT, True)
    if any(marker in text for marker in ("connection reset", "connection refused", "temporary failure", "server disconnected")):
        return ErrorInfo(ErrorCode.NETWORK_TIMEOUT, True)
    if "downloaderror" in name or "extractor" in text:
        return ErrorInfo(ErrorCode.EXTRACTOR_ERROR, False)
    if any(marker in text for marker in ("database", "sqlalchemy", "asyncpg")):
        return ErrorInfo(ErrorCode.DATABASE_ERROR, True)
    if any(marker in text for marker in ("no space left", "permission denied", "read-only file system")):
        return ErrorInfo(ErrorCode.STORAGE_ERROR, False)
    return ErrorInfo(ErrorCode.UNKNOWN, False)


def retry_delay(
    attempt: int,
    *,
    base: float = 4.0,
    cap: float = 45.0,
    jitter_ratio: float = 0.20,
    random_value: float | None = None,
) -> float:
    exponential = min(cap, base * (2 ** max(0, attempt)))
    rv = random.random() if random_value is None else max(0.0, min(1.0, random_value))
    jitter = exponential * max(0.0, jitter_ratio) * ((rv * 2.0) - 1.0)
    return max(0.0, exponential + jitter)
