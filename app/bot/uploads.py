from __future__ import annotations

import mimetypes
import uuid
from dataclasses import dataclass
from pathlib import Path

from aiogram.types import Message

from app.config import Settings, get_settings


@dataclass(frozen=True, slots=True)
class UploadCandidate:
    file_id: str
    file_name: str
    mime_type: str | None
    declared_type: str | None
    file_size: int | None


def upload_candidate(message: Message) -> UploadCandidate | None:
    if message.video is not None:
        video = message.video
        return UploadCandidate(
            file_id=video.file_id,
            file_name=video.file_name or "video.mp4",
            mime_type=video.mime_type or "video/mp4",
            declared_type="video",
            file_size=video.file_size,
        )
    if message.photo:
        photo = message.photo[-1]
        return UploadCandidate(
            file_id=photo.file_id,
            file_name="photo.jpg",
            mime_type="image/jpeg",
            declared_type="image",
            file_size=photo.file_size,
        )
    if message.audio is not None:
        audio = message.audio
        return UploadCandidate(
            file_id=audio.file_id,
            file_name=audio.file_name or _name_for_mime(audio.mime_type, "audio.m4a"),
            mime_type=audio.mime_type,
            declared_type="audio",
            file_size=audio.file_size,
        )
    if message.voice is not None:
        voice = message.voice
        return UploadCandidate(
            file_id=voice.file_id,
            file_name=_name_for_mime(voice.mime_type, "voice.ogg"),
            mime_type=voice.mime_type or "audio/ogg",
            declared_type="voice",
            file_size=voice.file_size,
        )
    if message.document is not None:
        document = message.document
        return UploadCandidate(
            file_id=document.file_id,
            file_name=document.file_name or _name_for_mime(document.mime_type, "document.bin"),
            mime_type=document.mime_type,
            declared_type=None,
            file_size=document.file_size,
        )
    return None


def _name_for_mime(mime_type: str | None, fallback: str) -> str:
    suffix = mimetypes.guess_extension(mime_type or "") or Path(fallback).suffix
    if suffix == ".jpe":
        suffix = ".jpg"
    return f"upload{suffix}" if suffix else fallback


def _safe_suffix(candidate: UploadCandidate) -> str:
    suffix = Path(candidate.file_name).suffix.lower()
    if 1 < len(suffix) <= 10 and suffix[1:].replace("_", "").isalnum():
        return suffix
    guessed = mimetypes.guess_extension(candidate.mime_type or "") or ""
    return ".jpg" if guessed == ".jpe" else guessed


async def download_upload(
    message: Message,
    candidate: UploadCandidate,
    *,
    settings: Settings | None = None,
) -> Path:
    cfg = settings or get_settings()
    if candidate.file_size is not None and candidate.file_size > cfg.max_file_size_bytes:
        raise ValueError("الملف أكبر من الحد المسموح للمشروع")
    if message.bot is None:
        raise RuntimeError("Telegram client is unavailable")
    root = cfg.render_temp_dir / f"telegram-upload-{uuid.uuid4().hex}"
    root.mkdir(parents=True, exist_ok=False)
    suffix = _safe_suffix(candidate)
    if not suffix:
        suffix = ".bin"
    target = root / f"source{suffix}"
    try:
        await message.bot.download(candidate.file_id, destination=target)
        if not target.exists() or target.stat().st_size <= 0:
            raise ValueError("Telegram returned an empty file")
        if target.stat().st_size > cfg.max_file_size_bytes:
            raise ValueError("الملف أكبر من الحد المسموح للمشروع")
        return target
    except Exception:
        if target.exists():
            target.unlink(missing_ok=True)
        root.rmdir()
        raise
