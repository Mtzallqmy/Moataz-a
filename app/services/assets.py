from __future__ import annotations

import asyncio
import json
import mimetypes
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import select

from app.config import Settings, get_settings
from app.db import MediaAsset, ProjectAsset, SessionLocal
from app.errors import FFmpegError
from app.services.downloader import DownloaderService, get_downloader_service
from app.services.media import _run_process

_ALLOWED_IMAGE = {".jpg", ".jpeg", ".png", ".webp"}
_ALLOWED_VIDEO = {".mp4", ".mov", ".mkv", ".webm", ".m4v"}
_ALLOWED_AUDIO = {".mp3", ".wav", ".m4a", ".ogg", ".opus", ".aac", ".flac"}
_ALLOWED_SUFFIXES = _ALLOWED_IMAGE | _ALLOWED_VIDEO | _ALLOWED_AUDIO
_ALLOWED_TYPES = {"video", "image", "audio", "voice", "logo", "subtitle"}


@dataclass(frozen=True, slots=True)
class AssetProbe:
    duration: float | None
    has_video: bool
    has_audio: bool
    width: int | None
    height: int | None
    codecs: tuple[str, ...]
    size_bytes: int


def _safe_child(root: Path, *parts: str) -> Path:
    root = root.resolve()
    candidate = root.joinpath(*parts).resolve()
    if candidate != root and root not in candidate.parents:
        raise ValueError("Unsafe storage path")
    return candidate


def _pick_suffix(source: Path, mime_type: str | None) -> str:
    suffix = source.suffix.lower()
    if suffix in _ALLOWED_SUFFIXES:
        return suffix
    guessed = mimetypes.guess_extension(mime_type or "") or ""
    if guessed == ".jpe":
        guessed = ".jpg"
    if guessed in _ALLOWED_SUFFIXES:
        return guessed
    raise ValueError("Unsupported media extension")


async def probe_asset_file(source: Path) -> AssetProbe:
    if not source.exists() or not source.is_file() or source.stat().st_size <= 0:
        raise ValueError("Media source file is missing or empty")
    stdout, _ = await _run_process(
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration,size:stream=codec_type,codec_name,width,height",
        "-of",
        "json",
        str(source),
        timeout=60,
    )
    try:
        payload = json.loads(stdout.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FFmpegError("FFprobe returned invalid asset metadata") from exc
    streams = payload.get("streams") or []
    has_video = any(item.get("codec_type") == "video" for item in streams)
    has_audio = any(item.get("codec_type") == "audio" for item in streams)
    video_stream = next((item for item in streams if item.get("codec_type") == "video"), {})
    raw_duration = payload.get("format", {}).get("duration")
    try:
        duration = float(raw_duration) if raw_duration not in {None, "", "N/A"} else None
    except (TypeError, ValueError):
        duration = None
    if duration is not None and duration <= 0:
        duration = None
    codecs = tuple(str(item.get("codec_name")) for item in streams if item.get("codec_name"))
    return AssetProbe(
        duration=duration,
        has_video=has_video,
        has_audio=has_audio,
        width=int(video_stream["width"]) if video_stream.get("width") else None,
        height=int(video_stream["height"]) if video_stream.get("height") else None,
        codecs=codecs,
        size_bytes=source.stat().st_size,
    )


def _infer_asset_type(
    *,
    declared_type: str | None,
    mime_type: str | None,
    suffix: str,
    probe: AssetProbe,
) -> str:
    requested = (declared_type or "").lower().strip()
    if requested and requested not in _ALLOWED_TYPES:
        raise ValueError("Unsupported media asset type")
    if not requested:
        mime = (mime_type or "").lower()
        if mime.startswith("image/") or suffix in _ALLOWED_IMAGE:
            requested = "image"
        elif mime.startswith("audio/") or suffix in _ALLOWED_AUDIO:
            requested = "audio"
        elif mime.startswith("video/") or suffix in _ALLOWED_VIDEO:
            requested = "video"
        else:
            raise ValueError("Unsupported document type")

    if requested in {"image", "logo"}:
        if suffix not in _ALLOWED_IMAGE or not probe.has_video:
            raise ValueError("File is not a supported image")
    elif requested in {"audio", "voice"}:
        if suffix not in _ALLOWED_AUDIO or not probe.has_audio or probe.has_video:
            raise ValueError("File is not a supported audio asset")
        if probe.duration is None:
            raise ValueError("Audio duration could not be determined")
    elif requested == "video":
        if suffix not in _ALLOWED_VIDEO or not probe.has_video or probe.duration is None:
            raise ValueError("File is not a supported video asset")
    elif requested == "subtitle":
        raise ValueError("Subtitle ingestion is not enabled in the MVP")
    return requested


class AssetService:
    """Media ingestion independent from Telegram/Aiogram."""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        downloader: DownloaderService | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.downloader = downloader or get_downloader_service()

    async def ingest_file(
        self,
        source: Path,
        *,
        user_id: int,
        project_id: int,
        declared_type: str | None = None,
        source_type: str = "local",
        mime_type: str | None = None,
        telegram_file_id: str | None = None,
        source_url: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> MediaAsset:
        source = Path(source)
        if source_type not in {"telegram", "url", "generated", "local"}:
            raise ValueError("Unsupported source type")
        if not source.exists() or not source.is_file():
            raise ValueError("Media source file is missing")
        if source.stat().st_size > self.settings.max_file_size_bytes:
            raise ValueError("File exceeds configured size limit")
        suffix = _pick_suffix(source, mime_type)
        probe = await probe_asset_file(source)
        asset_type = _infer_asset_type(
            declared_type=declared_type,
            mime_type=mime_type,
            suffix=suffix,
            probe=probe,
        )
        if probe.duration and probe.duration > self.settings.max_video_duration_seconds:
            raise ValueError("Media duration exceeds configured limit")

        guessed_mime = mime_type or mimetypes.guess_type(f"x{suffix}")[0]
        asset_key = uuid.uuid4().hex
        asset_dir = _safe_child(self.settings.project_dir, str(int(project_id)), "assets", asset_key)
        asset_dir.mkdir(parents=True, exist_ok=False)
        destination = _safe_child(asset_dir, f"source{suffix}")
        try:
            with source.open("rb") as src, destination.open("xb") as dst:
                shutil.copyfileobj(src, dst, length=1024 * 1024)
            if destination.stat().st_size != probe.size_bytes:
                raise OSError("Asset copy size mismatch")
            payload = {
                "codecs": list(probe.codecs),
                "has_video": probe.has_video,
                "has_audio": probe.has_audio,
                **(metadata or {}),
            }
            async with SessionLocal() as session:
                asset = MediaAsset(
                    user_id=user_id,
                    asset_type=asset_type,
                    source_type=source_type,
                    source_url=source_url,
                    telegram_file_id=telegram_file_id,
                    local_path=str(destination),
                    mime_type=guessed_mime,
                    duration=probe.duration,
                    width=probe.width,
                    height=probe.height,
                    file_size=destination.stat().st_size,
                    metadata_json=json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                )
                session.add(asset)
                await session.commit()
                await session.refresh(asset)
                session.expunge(asset)
                return asset
        except Exception:
            shutil.rmtree(asset_dir, ignore_errors=True)
            raise

    async def ingest_url(
        self,
        url: str,
        *,
        user_id: int,
        project_id: int,
        quality: str = "best",
    ) -> MediaAsset:
        ingest_id = uuid.uuid4().hex
        workspace = _safe_child(self.settings.render_temp_dir, f"ingest-{ingest_id}")
        workspace.mkdir(parents=True, exist_ok=False)
        job_key = f"studio-ingest:{ingest_id}"
        try:
            info = await asyncio.to_thread(self.downloader.probe, url)
            if info.is_playlist:
                raise ValueError("Use individual media URLs inside Media Studio")
            output = await asyncio.to_thread(
                self.downloader.download,
                url,
                quality,
                workspace,
                job_key=job_key,
                known_qualities=info.qualities,
            )
            declared = "audio" if quality.lower() in {"audio", "mp3"} else None
            return await self.ingest_file(
                output,
                user_id=user_id,
                project_id=project_id,
                declared_type=declared,
                source_type="url",
                source_url=info.webpage_url or url,
                metadata={"title": info.title, "platform": info.platform, "uploader": info.uploader},
            )
        finally:
            self.downloader.forget(job_key)
            shutil.rmtree(workspace, ignore_errors=True)

    async def get_asset(self, asset_id: int, *, user_id: int | None = None) -> MediaAsset | None:
        async with SessionLocal() as session:
            statement = select(MediaAsset).where(MediaAsset.id == asset_id)
            if user_id is not None:
                statement = statement.where(MediaAsset.user_id == user_id)
            asset = await session.scalar(statement)
            if asset is not None:
                session.expunge(asset)
            return asset

    async def delete_unattached_asset(self, asset_id: int, *, user_id: int) -> bool:
        async with SessionLocal() as session:
            asset = await session.scalar(
                select(MediaAsset).where(MediaAsset.id == asset_id, MediaAsset.user_id == user_id)
            )
            if asset is None:
                return False
            linked = await session.scalar(
                select(ProjectAsset.project_id).where(ProjectAsset.asset_id == asset_id).limit(1)
            )
            if linked is not None:
                raise ValueError("Asset is attached to a project")
            path = Path(asset.local_path)
            await session.delete(asset)
            await session.commit()
        shutil.rmtree(path.parent, ignore_errors=True)
        return True


asset_service = AssetService()
