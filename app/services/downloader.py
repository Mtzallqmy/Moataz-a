from __future__ import annotations

import base64
import binascii
import logging
import os
import re
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import yt_dlp
from yt_dlp.networking.impersonate import ImpersonateTarget

from app.config import Settings, get_settings
from app.errors import CancelledError, ErrorCode, ErrorInfo, FormatUnavailableError, classify_error
from app.security import assert_public_dns, canonicalize_url, redact_secrets
from app.services.download_relays import CobaltRelayClient
from app.services.providers import available_qualities, format_selector, normalize_platform

ProgressHook = Callable[[dict[str, Any]], None]
UrlGuard = Callable[[str], str]
logger = logging.getLogger("moataz.downloader")


class _SanitizedYTDLPLogger:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def _safe(self, message: object) -> str:
        return redact_secrets(
            message,
            bot_token=self.settings.bot_token,
            database_url=self.settings.database_url,
        )[:4000]

    def debug(self, message: object) -> None:
        text = self._safe(message)
        if text.startswith("[debug]"):
            logger.debug("yt-dlp: %s", text)

    def warning(self, message: object) -> None:
        logger.warning("yt-dlp: %s", self._safe(message))

    def error(self, message: object) -> None:
        logger.error("yt-dlp: %s", self._safe(message))


@dataclass(frozen=True, slots=True)
class MediaFormat:
    format_id: str
    height: int
    ext: str | None = None
    fps: float | None = None
    tbr: float | None = None


@dataclass(slots=True)
class MediaInfo:
    title: str
    thumbnail: str | None
    duration: int | None
    uploader: str | None
    platform: str
    formats: list[MediaFormat] = field(default_factory=list)
    qualities: list[int] = field(default_factory=list)
    webpage_url: str | None = None
    media_id: str | None = None
    extractor: str | None = None
    is_playlist: bool = False
    playlist_count: int = 0


@dataclass(frozen=True, slots=True)
class PlaylistEntry:
    url: str
    title: str | None = None
    index: int | None = None


@dataclass(frozen=True, slots=True)
class DownloadAttempt:
    name: str
    options: dict[str, Any]


class DownloaderService:
    """Generic, testable yt-dlp engine used by Telegram and the dashboard."""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        ydl_factory: Callable[[dict[str, Any]], Any] = yt_dlp.YoutubeDL,
        url_guard: UrlGuard = assert_public_dns,
        relay: CobaltRelayClient | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.ydl_factory = ydl_factory
        self.url_guard = url_guard
        self.relay = relay or CobaltRelayClient(self.settings, url_guard=url_guard)
        self._cancel_events: dict[str, threading.Event] = {}
        self._lock = threading.Lock()

    def _base_options(self) -> dict[str, Any]:
        options: dict[str, Any] = {
            "quiet": True,
            "no_warnings": True,
            "logger": _SanitizedYTDLPLogger(self.settings),
            "socket_timeout": self.settings.ytdlp_socket_timeout_seconds,
            "retries": self.settings.ytdlp_retries,
            "fragment_retries": self.settings.ytdlp_fragment_retries,
            "extractor_retries": self.settings.ytdlp_retries,
            "concurrent_fragment_downloads": self.settings.ytdlp_concurrent_fragments,
            "cachedir": False,
            "geo_bypass": False,
            "nocheckcertificate": False,
            "restrictfilenames": True,
            "ignoreerrors": False,
            "http_headers": {
                "Accept-Language": "en-US,en;q=0.9",
            },
        }
        cookie_file = self._cookie_file()
        if cookie_file is not None:
            options["cookiefile"] = str(cookie_file)
        return options

    def _cookie_file(self) -> Path | None:
        configured = self.settings.ytdlp_cookies_file
        if configured is not None:
            path = configured.expanduser().resolve()
            if not path.is_file():
                raise ValueError("Configured yt-dlp cookies file does not exist")
            return path

        encoded = self.settings.ytdlp_cookies_b64
        if encoded is None or not encoded.get_secret_value().strip():
            return None
        try:
            payload = base64.b64decode(encoded.get_secret_value().strip(), validate=True)
        except (ValueError, binascii.Error) as exc:
            raise ValueError("YTDLP_COOKIES_B64 is not valid base64") from exc
        if len(payload) > 2 * 1024 * 1024:
            raise ValueError("YTDLP_COOKIES_B64 exceeds the 2 MiB safety limit")
        if b"Netscape HTTP Cookie File" not in payload[:256]:
            raise ValueError("YTDLP_COOKIES_B64 must contain a Netscape cookies.txt file")
        with self._lock:
            credential_dir = (self.settings.render_temp_dir / "credentials").resolve()
            credential_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(credential_dir, 0o700)
            destination = credential_dir / "yt-dlp-cookies.txt"
            temporary = credential_dir / "yt-dlp-cookies.tmp"
            temporary.write_bytes(payload)
            os.chmod(temporary, 0o600)
            temporary.replace(destination)
            os.chmod(destination, 0o600)
        return destination

    def _proxy_urls(self) -> tuple[str, ...]:
        secret = self.settings.ytdlp_proxy_urls
        raw = secret.get_secret_value().strip() if secret is not None else ""
        result: list[str] = []
        for candidate in re.split(r"[,\n]", raw):
            value = candidate.strip()
            if not value or value in result:
                continue
            parsed = urlsplit(value)
            if parsed.scheme not in {"http", "https", "socks5", "socks5h"} or not parsed.hostname:
                raise ValueError("YTDLP_PROXY_URLS contains an invalid proxy URL")
            result.append(value)
        if len(result) > 4:
            raise ValueError("At most four yt-dlp proxy URLs may be configured")
        return tuple(result)

    def _attempts(self) -> tuple[DownloadAttempt, ...]:
        attempts = [DownloadAttempt("direct", {})]
        browser_options: dict[str, Any] = {}
        if self.ydl_factory is yt_dlp.YoutubeDL:
            try:
                import curl_cffi  # noqa: F401
            except ImportError:
                pass
            else:
                browser_options = (
                    {"impersonate": ImpersonateTarget(client="chrome")}
                    if self.settings.ytdlp_impersonate
                    else {}
                )
                if browser_options:
                    attempts.append(DownloadAttempt("browser", browser_options))
        for index, proxy in enumerate(self._proxy_urls(), start=1):
            options: dict[str, Any] = {"proxy": proxy}
            options.update(browser_options)
            attempts.append(DownloadAttempt(f"proxy-{index}", options))
        return tuple(attempts)

    @staticmethod
    def _can_try_next(exc: BaseException) -> bool:
        return classify_error(exc).code in {
            ErrorCode.ANTI_BOT,
            ErrorCode.HTTP_429,
            ErrorCode.UPSTREAM_5XX,
            ErrorCode.NETWORK_TIMEOUT,
            ErrorCode.EXTRACTOR_ERROR,
        }

    @staticmethod
    def _can_try_relay(exc: BaseException) -> bool:
        return (
            DownloaderService._can_try_next(exc)
            or classify_error(exc).code is ErrorCode.UNSUPPORTED_EXTRACTOR
        )

    def _log_failure(
        self, operation: str, url: str, exc: BaseException, *, backend: str = "direct"
    ) -> None:
        error = classify_error(exc)
        host = canonicalize_url(url).split("/", 3)[2] if "://" in url else "unknown"
        safe = redact_secrets(
            exc,
            bot_token=self.settings.bot_token,
            database_url=self.settings.database_url,
        )[:4000]
        for proxy in self._proxy_urls():
            safe = safe.replace(proxy, "[REDACTED_PROXY]")
        logger.warning(
            "yt-dlp %s failed host=%s code=%s retryable=%s error_type=%s detail=%s",
            f"{operation}/{backend}",
            host,
            error.code.value,
            error.retryable,
            type(exc).__name__,
            safe,
        )

    def _guard(self, url: str) -> str:
        return self.url_guard(url)

    def _event(self, job_key: str) -> threading.Event:
        with self._lock:
            return self._cancel_events.setdefault(job_key, threading.Event())

    def cancel(self, job_key: str) -> bool:
        with self._lock:
            event = self._cancel_events.get(job_key)
            if event is None:
                event = threading.Event()
                self._cancel_events[job_key] = event
            already = event.is_set()
            event.set()
        return not already

    def _release(self, job_key: str) -> None:
        with self._lock:
            self._cancel_events.pop(job_key, None)

    def forget(self, job_key: str) -> None:
        """Release process-local cancellation state after a job reaches a terminal state."""
        self._release(job_key)

    def _safe_ydl(self, options: dict[str, Any]):
        service = self
        factory = self.ydl_factory
        if factory is not yt_dlp.YoutubeDL:
            return factory(options)

        class SafeYoutubeDL(yt_dlp.YoutubeDL):
            def urlopen(self, req):  # type: ignore[override]
                target = getattr(req, "url", None) or getattr(req, "full_url", None) or str(req)
                service._guard(target)
                return super().urlopen(req)

        return SafeYoutubeDL(options)

    def probe(self, url: str) -> MediaInfo:
        guarded = self._guard(url)
        info = None
        last_error: Exception | None = None
        for attempt in self._attempts():
            opts = self._base_options()
            opts.update(attempt.options)
            opts.update(
                {
                    "skip_download": True,
                    "noplaylist": False,
                    "extract_flat": "in_playlist",
                    "playlistend": self.settings.max_playlist_items + 1,
                }
            )
            try:
                with self._safe_ydl(opts) as ydl:
                    info = ydl.extract_info(guarded, download=False)
                break
            except Exception as exc:
                last_error = exc
                self._log_failure("probe", guarded, exc, backend=attempt.name)
                if not self._can_try_next(exc):
                    break
        if info is None and last_error is not None and self.relay.enabled and self._can_try_relay(last_error):
            try:
                fallback = self.relay.probe(guarded)
                return MediaInfo(
                    title=fallback.title,
                    thumbnail=None,
                    duration=None,
                    uploader=None,
                    platform=fallback.platform,
                    qualities=[],
                    webpage_url=guarded,
                    extractor=f"cobalt-{fallback.endpoint_index}",
                )
            except Exception as exc:
                self._log_failure("probe", guarded, exc, backend="cobalt")
        if info is None and last_error is not None:
            raise last_error
        if not info:
            raise RuntimeError("yt-dlp returned no media metadata")

        if info.get("_type") in {"playlist", "multi_video"}:
            entries = [entry for entry in info.get("entries") or [] if entry]
            return MediaInfo(
                title=(info.get("title") or "Playlist")[:500],
                thumbnail=info.get("thumbnail"),
                duration=None,
                uploader=info.get("uploader") or info.get("channel"),
                platform=normalize_platform(info.get("extractor_key") or info.get("extractor"), guarded),
                webpage_url=info.get("webpage_url") or guarded,
                media_id=str(info.get("id"))[:128] if info.get("id") else None,
                extractor=info.get("extractor_key") or info.get("extractor"),
                is_playlist=True,
                playlist_count=len(entries),
            )

        return self._media_info(info, guarded)

    def _media_info(self, info: dict[str, Any], source_url: str) -> MediaInfo:
        formats = self.get_formats(info)
        return MediaInfo(
            title=(info.get("title") or "Untitled")[:500],
            thumbnail=info.get("thumbnail"),
            duration=int(info["duration"]) if info.get("duration") else None,
            uploader=(info.get("uploader") or info.get("channel") or None),
            platform=normalize_platform(info.get("extractor_key") or info.get("extractor"), source_url),
            formats=formats,
            qualities=sorted({fmt.height for fmt in formats}),
            webpage_url=info.get("webpage_url") or source_url,
            media_id=str(info.get("id"))[:128] if info.get("id") else None,
            extractor=info.get("extractor_key") or info.get("extractor"),
        )

    def get_formats(self, info_or_url: dict[str, Any] | str) -> list[MediaFormat]:
        if isinstance(info_or_url, str):
            media = self.probe(info_or_url)
            return media.formats
        info = info_or_url
        best_by_height: dict[int, dict[str, Any]] = {}
        for fmt in info.get("formats") or []:
            height = fmt.get("height")
            if not height or fmt.get("vcodec") in {None, "none"}:
                continue
            height = int(height)
            score = float(fmt.get("tbr") or 0)
            current = best_by_height.get(height)
            if current is None or score > float(current.get("tbr") or 0):
                best_by_height[height] = fmt
        allowed = set(available_qualities(info))
        result = [
            MediaFormat(
                format_id=str(fmt.get("format_id") or ""),
                height=height,
                ext=fmt.get("ext"),
                fps=float(fmt["fps"]) if fmt.get("fps") else None,
                tbr=float(fmt["tbr"]) if fmt.get("tbr") else None,
            )
            for height, fmt in best_by_height.items()
            if height in allowed
        ]
        return sorted(result, key=lambda item: item.height)

    def expand_playlist(self, url: str, *, limit: int | None = None) -> list[PlaylistEntry]:
        guarded = self._guard(url)
        max_items = self.settings.max_playlist_items if limit is None else min(limit, self.settings.max_playlist_items)
        info = None
        last_error: Exception | None = None
        for attempt in self._attempts():
            opts = self._base_options()
            opts.update(attempt.options)
            opts.update(
                {
                    "skip_download": True,
                    "extract_flat": True,
                    "noplaylist": False,
                    "playlistend": max_items + 1,
                }
            )
            try:
                with self._safe_ydl(opts) as ydl:
                    info = ydl.extract_info(guarded, download=False)
                break
            except Exception as exc:
                last_error = exc
                self._log_failure("playlist probe", guarded, exc, backend=attempt.name)
                if not self._can_try_next(exc):
                    break
        if info is None and last_error is not None:
            raise last_error
        if not info or info.get("_type") not in {"playlist", "multi_video"}:
            return [PlaylistEntry(url=guarded, title=info.get("title") if info else None, index=1)]
        entries = [entry for entry in info.get("entries") or [] if entry]
        if len(entries) > max_items:
            raise ValueError(f"Playlist exceeds safe limit of {max_items} items")
        result: list[PlaylistEntry] = []
        seen: set[str] = set()
        for index, entry in enumerate(entries, start=1):
            candidate = entry.get("webpage_url") or entry.get("original_url") or entry.get("url")
            if not candidate or not str(candidate).startswith(("http://", "https://")):
                continue
            safe = self._guard(str(candidate))
            key = canonicalize_url(safe)
            if key in seen:
                continue
            seen.add(key)
            result.append(PlaylistEntry(url=safe, title=entry.get("title"), index=index))
        return result

    def download(
        self,
        url: str,
        quality: str,
        job_dir: Path,
        *,
        job_key: str,
        progress_hook: ProgressHook | None = None,
        known_qualities: list[int] | None = None,
    ) -> Path:
        guarded = self._guard(url)
        value = quality.lower().strip().removesuffix("p")
        if value not in {"best", "audio", "mp3"}:
            height = int(value)
            if known_qualities is not None and height not in known_qualities:
                raise FormatUnavailableError(f"Requested format {height}p is not available")

        event = self._event(job_key)
        job_dir.mkdir(parents=True, exist_ok=True)

        def guarded_progress(payload: dict[str, Any]) -> None:
            if event.is_set():
                raise CancelledError("Download cancelled")
            if progress_hook:
                progress_hook(payload)

        try:
            last_error: Exception | None = None
            for attempt in self._attempts():
                opts = self._base_options()
                opts.update(attempt.options)
                opts.update(
                    {
                        "noplaylist": True,
                        "format": format_selector(value),
                        "outtmpl": str(job_dir / "%(id)s-%(title).80B.%(ext)s"),
                        "merge_output_format": "mp4",
                        "prefer_ffmpeg": True,
                        "continuedl": True,
                        "overwrites": False,
                        "nopart": False,
                        "keepvideo": False,
                        "max_filesize": self.settings.max_file_size_bytes,
                        "progress_hooks": [guarded_progress],
                    }
                )
                if value in {"audio", "mp3"}:
                    opts["postprocessors"] = [
                        {
                            "key": "FFmpegExtractAudio",
                            "preferredcodec": "mp3",
                            "preferredquality": "192",
                        }
                    ]
                try:
                    with self._safe_ydl(opts) as ydl:
                        ydl.download([guarded])
                    if event.is_set():
                        raise CancelledError("Download cancelled")
                    suffixes = (
                        {".mp3"}
                        if value in {"audio", "mp3"}
                        else {".mp4", ".mkv", ".webm"}
                    )
                    candidates = [
                        path
                        for path in job_dir.iterdir()
                        if path.is_file()
                        and path.suffix.lower() in suffixes
                        and not path.name.endswith((".part", ".ytdl"))
                    ]
                    if not candidates:
                        raise RuntimeError("yt-dlp finished without a valid output file")
                    output = max(candidates, key=lambda path: path.stat().st_size)
                    if output.stat().st_size <= 0:
                        raise RuntimeError("yt-dlp produced an empty output file")
                    return output
                except Exception as exc:
                    last_error = exc
                    self._log_failure("download", guarded, exc, backend=attempt.name)
                    if isinstance(exc, CancelledError) or not self._can_try_next(exc):
                        break
            if (
                last_error is not None
                and self.relay.enabled
                and self._can_try_relay(last_error)
            ):
                try:
                    return self.relay.download(
                        guarded,
                        value,
                        job_dir,
                        cancel_event=event,
                        progress_hook=guarded_progress,
                    )
                except Exception as exc:
                    self._log_failure("download", guarded, exc, backend="cobalt")
            if last_error is not None:
                raise last_error
            raise RuntimeError("All download backends failed")
        finally:
            self._release(job_key)

    def download_audio(self, url: str, job_dir: Path, *, job_key: str, progress_hook: ProgressHook | None = None) -> Path:
        return self.download(url, "audio", job_dir, job_key=job_key, progress_hook=progress_hook)

    @staticmethod
    def classify_error(exc: BaseException) -> ErrorInfo:
        return classify_error(exc)


def get_downloader_service() -> DownloaderService:
    # Module-level singleton without any network or filesystem side effects.
    global _DOWNLOADER_SINGLETON
    try:
        return _DOWNLOADER_SINGLETON
    except NameError:
        _DOWNLOADER_SINGLETON = DownloaderService()
        return _DOWNLOADER_SINGLETON
