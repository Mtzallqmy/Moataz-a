from __future__ import annotations

import random
from dataclasses import dataclass
from enum import StrEnum


class ErrorCode(StrEnum):
    INVALID_URL = "INVALID_URL"
    MEDIA_UNAVAILABLE = "MEDIA_UNAVAILABLE"
    PRIVATE_MEDIA = "PRIVATE_MEDIA"
    AUTH_REQUIRED = "AUTH_REQUIRED"
    ANTI_BOT = "ANTI_BOT"
    BOT_CHALLENGE = "ANTI_BOT"
    UNSUPPORTED_EXTRACTOR = "UNSUPPORTED_EXTRACTOR"
    UNSUPPORTED_URL = "UNSUPPORTED_EXTRACTOR"
    FORMAT_UNAVAILABLE = "FORMAT_UNAVAILABLE"
    EXTRACTOR_ERROR = "EXTRACTOR_ERROR"
    NETWORK_TIMEOUT = "NETWORK_TIMEOUT"
    HTTP_403 = "HTTP_403"
    HTTP_429 = "HTTP_429"
    UPSTREAM_5XX = "UPSTREAM_5XX"
    UPSTREAM_ERROR = "UPSTREAM_5XX"
    BACKEND_UNAVAILABLE = "BACKEND_UNAVAILABLE"
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
    if any(marker in text for marker in ("confirm you're not a bot", "confirm you’re not a bot", "captcha", "unusual traffic", "bot challenge")):
        return ErrorInfo(ErrorCode.ANTI_BOT, False)
    if "private" in text or "members-only" in text:
        return ErrorInfo(ErrorCode.PRIVATE_MEDIA, False)
    if any(marker in text for marker in ("login required", "sign in to view", "authentication required", "cookies required", "use --cookies")):
        return ErrorInfo(ErrorCode.AUTH_REQUIRED, False)
    if any(marker in text for marker in ("unsupported url", "no suitable extractor", "is not a valid url")):
        return ErrorInfo(ErrorCode.UNSUPPORTED_EXTRACTOR, False)
    if any(marker in text for marker in ("invalid url", "unsafe url", "ssrf")):
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
    if any(marker in text for marker in ("http error 403", "http 403", "status 403", "forbidden")):
        return ErrorInfo(ErrorCode.HTTP_403, True)
    if any(marker in text for marker in ("500", "502", "503", "504", "service unavailable", "bad gateway")):
        return ErrorInfo(ErrorCode.UPSTREAM_5XX, True)
    if any(marker in text for marker in ("timeout", "timed out", "socket timeout")):
        return ErrorInfo(ErrorCode.NETWORK_TIMEOUT, True)
    if any(marker in text for marker in ("connection reset", "connection refused", "temporary failure", "server disconnected")):
        return ErrorInfo(ErrorCode.NETWORK_TIMEOUT, True)
    if any(marker in text for marker in ("cannot parse data", "unable to extract", "temporary extractor")):
        return ErrorInfo(ErrorCode.EXTRACTOR_ERROR, True)
    if "downloaderror" in name or "extractor" in text:
        return ErrorInfo(ErrorCode.EXTRACTOR_ERROR, False)
    if any(marker in text for marker in ("database", "sqlalchemy", "asyncpg")):
        return ErrorInfo(ErrorCode.DATABASE_ERROR, True)
    if any(marker in text for marker in ("no space left", "permission denied", "read-only file system")):
        return ErrorInfo(ErrorCode.STORAGE_ERROR, False)
    return ErrorInfo(ErrorCode.UNKNOWN, False)


_USER_MESSAGES = {
    "ar": {
        ErrorCode.INVALID_URL: "الرابط غير صالح أو محظور لأسباب أمنية.",
        ErrorCode.MEDIA_UNAVAILABLE: "الفيديو غير متاح أو تمت إزالته من المنصة.",
        ErrorCode.PRIVATE_MEDIA: "الفيديو خاص أو لا يملك الحساب صلاحية الوصول إليه.",
        ErrorCode.AUTH_REQUIRED: "هذا الرابط يحتاج تسجيل دخول أو Cookies صالحة.",
        ErrorCode.ANTI_BOT: "تعذر اجتياز تحقق المنصة ضد الروبوتات بعد تجربة مسارات التحميل المتاحة. حدّث Cookies أو فعّل Proxy/Cobalt احتياطيًا ثم حاول مجددًا.",
        ErrorCode.UNSUPPORTED_EXTRACTOR: "هذه المنصة أو صيغة الرابط غير مدعومة حاليًا.",
        ErrorCode.FORMAT_UNAVAILABLE: "الجودة المطلوبة غير متاحة لهذا الفيديو.",
        ErrorCode.EXTRACTOR_ERROR: "تعذر استخراج الرابط من المنصة، جرّب لاحقًا أو حدّث الرابط.",
        ErrorCode.NETWORK_TIMEOUT: "انتهت مهلة الاتصال بالمنصة. ستتم المحاولة لاحقًا.",
        ErrorCode.HTTP_429: "المنصة حدّت عدد الطلبات مؤقتًا. حاول بعد قليل.",
        ErrorCode.HTTP_403: "رفضت المنصة مسار الاتصال الحالي. تمت تجربة البدائل المتاحة.",
        ErrorCode.UPSTREAM_5XX: "المنصة تواجه عطلًا مؤقتًا. حاول لاحقًا.",
        ErrorCode.BACKEND_UNAVAILABLE: "لا يتوفر حاليًا محرك تحميل مناسب لهذا الرابط.",
    },
    "en": {
        ErrorCode.INVALID_URL: "The URL is invalid or blocked for security reasons.",
        ErrorCode.MEDIA_UNAVAILABLE: "The media is unavailable or was removed.",
        ErrorCode.PRIVATE_MEDIA: "The media is private or the account cannot access it.",
        ErrorCode.AUTH_REQUIRED: "This URL requires login or valid cookies.",
        ErrorCode.ANTI_BOT: "The platform anti-bot check blocked all available download routes. Refresh cookies or configure a proxy/Cobalt fallback and retry.",
        ErrorCode.UNSUPPORTED_EXTRACTOR: "This platform or URL format is not currently supported.",
        ErrorCode.FORMAT_UNAVAILABLE: "The requested quality is unavailable for this media.",
        ErrorCode.EXTRACTOR_ERROR: "The platform could not be parsed. Try later or use an updated URL.",
        ErrorCode.NETWORK_TIMEOUT: "The platform connection timed out. Please try later.",
        ErrorCode.HTTP_429: "The platform is temporarily rate-limiting requests. Try again later.",
        ErrorCode.HTTP_403: "The platform rejected the current route. Available fallbacks were tried.",
        ErrorCode.UPSTREAM_5XX: "The platform is temporarily unavailable. Try again later.",
        ErrorCode.BACKEND_UNAVAILABLE: "No suitable download backend is currently available.",
    },
}


def user_error_message(error: ErrorInfo | ErrorCode, language: str = "ar") -> str:
    code = error.code if isinstance(error, ErrorInfo) else error
    table = _USER_MESSAGES.get(language, _USER_MESSAGES["ar"])
    fallback = (
        "تعذر تنفيذ الطلب بسبب خطأ غير متوقع. حاول مرة أخرى."
        if language != "en"
        else "The request failed unexpectedly. Please try again."
    )
    return table.get(code, fallback)


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
