from __future__ import annotations

import asyncio
import hashlib
import json
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from sqlalchemy import select

from app.config import Settings, get_settings
from app.db import MediaAsset, MediaAssetAnalysis, ProjectAsset, SessionLocal
from app.services.media import _run_process

ANALYZER_VERSION = "media-intelligence-v1"
_SILENCE_RE = re.compile(r"silence_(start|end):\s*([0-9.]+)")
_SCENE_RE = re.compile(r"pts_time:([0-9.]+)")
_MEAN_VOLUME_RE = re.compile(r"mean_volume:\s*(-?[0-9.]+) dB")
_MAX_VOLUME_RE = re.compile(r"max_volume:\s*(-?[0-9.]+) dB")
_locks: dict[int, asyncio.Lock] = {}


class Transcriber(Protocol):
    async def transcribe(self, source: Path) -> dict[str, Any]: ...


class VisionAnalyzer(Protocol):
    async def describe(self, frames: list[Path]) -> list[dict[str, Any]]: ...


@dataclass(frozen=True, slots=True)
class AssetIntelligence:
    asset_id: int
    status: str
    payload: dict[str, Any]
    cached: bool


def _fingerprint(path: Path) -> str:
    stat = path.stat()
    digest = hashlib.sha256()
    digest.update(f"{stat.st_size}:{stat.st_mtime_ns}".encode())
    with path.open("rb") as stream:
        digest.update(stream.read(1024 * 1024))
        if stat.st_size > 1024 * 1024:
            stream.seek(max(0, stat.st_size - 1024 * 1024))
            digest.update(stream.read(1024 * 1024))
    return digest.hexdigest()


def _complement(duration: float, ranges: list[dict[str, float]]) -> list[dict[str, float]]:
    result: list[dict[str, float]] = []
    cursor = 0.0
    for item in sorted(ranges, key=lambda value: value["start"]):
        start = max(cursor, min(duration, item["start"]))
        if start - cursor >= 0.05:
            result.append({"start": round(cursor, 3), "end": round(start, 3)})
        cursor = max(cursor, min(duration, item["end"]))
    if duration - cursor >= 0.05:
        result.append({"start": round(cursor, 3), "end": round(duration, 3)})
    return result


def _parse_silence(stderr: str, duration: float) -> list[dict[str, float]]:
    ranges: list[dict[str, float]] = []
    current: float | None = None
    for kind, raw_value in _SILENCE_RE.findall(stderr):
        value = max(0.0, min(duration, float(raw_value)))
        if kind == "start":
            current = value
        elif current is not None and value - current >= 0.05:
            ranges.append({"start": round(current, 3), "end": round(value, 3)})
            current = None
    if current is not None and duration - current >= 0.05:
        ranges.append({"start": round(current, 3), "end": round(duration, 3)})
    return ranges


class MediaIntelligenceService:
    """Cache safe, reusable media understanding separately from render state."""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        transcriber: Transcriber | None = None,
        vision: VisionAnalyzer | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.transcriber = transcriber
        self.vision = vision

    async def get(self, asset_id: int, *, user_id: int) -> AssetIntelligence | None:
        async with SessionLocal() as session:
            row = await session.scalar(
                select(MediaAssetAnalysis).where(
                    MediaAssetAnalysis.asset_id == asset_id,
                    MediaAssetAnalysis.user_id == user_id,
                )
            )
            if row is None:
                return None
            try:
                payload = json.loads(row.analysis_json or "{}")
            except json.JSONDecodeError:
                payload = {}
            return AssetIntelligence(asset_id, row.status, payload, True)

    async def analyze_asset(
        self,
        asset_id: int,
        *,
        user_id: int,
        project_id: int,
        force: bool = False,
    ) -> AssetIntelligence:
        lock = _locks.setdefault(asset_id, asyncio.Lock())
        async with lock:
            asset = await self._owned_asset(asset_id, user_id=user_id, project_id=project_id)
            source = Path(asset.local_path).resolve()
            storage_root = self.settings.project_dir.resolve()
            if storage_root not in source.parents or not source.is_file():
                raise ValueError("Asset analysis source is outside project storage")
            fingerprint = await asyncio.to_thread(_fingerprint, source)
            cached = await self.get(asset_id, user_id=user_id)
            if (
                not force
                and cached is not None
                and cached.status == "COMPLETED"
                and cached.payload.get("source_fingerprint") == fingerprint
                and cached.payload.get("analyzer_version") == ANALYZER_VERSION
            ):
                return cached
            await self._set_state(
                asset,
                fingerprint=fingerprint,
                status="ANALYZING",
                payload={},
                error=None,
            )
            try:
                payload = await self._analyze(asset, source, project_id, fingerprint)
                await self._set_state(
                    asset,
                    fingerprint=fingerprint,
                    status="COMPLETED",
                    payload=payload,
                    error=None,
                )
                return AssetIntelligence(asset_id, "COMPLETED", payload, False)
            except Exception as exc:
                await self._set_state(
                    asset,
                    fingerprint=fingerprint,
                    status="FAILED",
                    payload={},
                    error=f"{type(exc).__name__}: {exc}"[:2000],
                )
                raise

    async def analyze_project(
        self,
        project_id: int,
        *,
        user_id: int,
        force: bool = False,
    ) -> dict[str, Any]:
        async with SessionLocal() as session:
            asset_ids = list(
                await session.scalars(
                    select(ProjectAsset.asset_id)
                    .join(MediaAsset, MediaAsset.id == ProjectAsset.asset_id)
                    .where(
                        ProjectAsset.project_id == project_id,
                        MediaAsset.user_id == user_id,
                    )
                    .order_by(ProjectAsset.position)
                )
            )
        analyses = []
        for asset_id in asset_ids:
            analyses.append(
                (
                    await self.analyze_asset(
                        asset_id,
                        user_id=user_id,
                        project_id=project_id,
                        force=force,
                    )
                ).payload
            )
        return {
            "version": 1,
            "project_id": project_id,
            "assets": analyses,
            "duration": round(
                sum(float(item.get("quality", {}).get("duration") or 0) for item in analyses),
                3,
            ),
        }

    async def _owned_asset(
        self, asset_id: int, *, user_id: int, project_id: int
    ) -> MediaAsset:
        async with SessionLocal() as session:
            asset = await session.scalar(
                select(MediaAsset)
                .join(ProjectAsset, ProjectAsset.asset_id == MediaAsset.id)
                .where(
                    MediaAsset.id == asset_id,
                    MediaAsset.user_id == user_id,
                    ProjectAsset.project_id == project_id,
                )
            )
            if asset is None:
                raise LookupError("Project asset not found")
            session.expunge(asset)
            return asset

    async def _set_state(
        self,
        asset: MediaAsset,
        *,
        fingerprint: str,
        status: str,
        payload: dict[str, Any],
        error: str | None,
    ) -> None:
        async with SessionLocal() as session:
            row = await session.get(MediaAssetAnalysis, asset.id)
            if row is None:
                row = MediaAssetAnalysis(
                    asset_id=asset.id,
                    user_id=asset.user_id,
                    analyzer_version=ANALYZER_VERSION,
                    source_fingerprint=fingerprint,
                )
                session.add(row)
            row.analyzer_version = ANALYZER_VERSION
            row.source_fingerprint = fingerprint
            row.status = status
            row.analysis_json = json.dumps(
                payload, ensure_ascii=False, separators=(",", ":")
            )
            row.error = error
            await session.commit()

    async def _analyze(
        self,
        asset: MediaAsset,
        source: Path,
        project_id: int,
        fingerprint: str,
    ) -> dict[str, Any]:
        duration = float(asset.duration or 0)
        try:
            source_metadata = json.loads(asset.metadata_json or "{}")
        except json.JSONDecodeError:
            source_metadata = {}
        analysis_dir = (
            self.settings.project_dir
            / str(project_id)
            / "analysis"
            / str(asset.id)
            / fingerprint[:16]
        ).resolve()
        root = self.settings.project_dir.resolve()
        if root not in analysis_dir.parents:
            raise ValueError("Unsafe analysis workspace")
        if analysis_dir.exists():
            shutil.rmtree(analysis_dir)
        analysis_dir.mkdir(parents=True, exist_ok=False)
        silence: list[dict[str, float]] = []
        loudness: dict[str, float | None] = {"mean_db": None, "max_db": None}
        has_audio = asset.asset_type in {"audio", "voice"} or bool(
            source_metadata.get("has_audio")
        )
        if has_audio:
            silence = await self._silence(source, duration)
            loudness = await self._loudness(source)
        scenes: list[dict[str, Any]] = []
        if asset.asset_type in {"video", "image", "logo"}:
            scenes = await self._scenes(
                source, duration if duration > 0 else 4.0, analysis_dir
            )
        transcription: dict[str, Any] = {
            "available": False,
            "segments": [],
            "words": [],
        }
        if self.transcriber is not None and has_audio:
            candidate = await self.transcriber.transcribe(source)
            if not isinstance(candidate, dict):
                raise ValueError("Transcriber returned an invalid result")
            transcription = {"available": True, **candidate}
        descriptions: list[dict[str, Any]] = []
        if self.vision is not None and scenes:
            frame_paths = [Path(item["thumbnail"]) for item in scenes if item.get("thumbnail")]
            descriptions = await self.vision.describe(frame_paths)
            for scene, description in zip(scenes, descriptions, strict=False):
                if isinstance(description, dict):
                    scene["description"] = str(description.get("description") or "")[:1000]
                    scene["focus"] = description.get("focus")
        speech = _complement(duration, silence) if duration > 0 else []
        important = sorted(
            scenes,
            key=lambda item: (-float(item.get("score") or 0), float(item.get("start") or 0)),
        )[:8]
        return {
            "version": 1,
            "analyzer_version": ANALYZER_VERSION,
            "source_fingerprint": fingerprint,
            "asset_id": asset.id,
            "asset_type": asset.asset_type,
            "quality": {
                "duration": duration,
                "width": asset.width,
                "height": asset.height,
                "file_size": asset.file_size,
                "mime_type": asset.mime_type,
            },
            "audio": {
                "silence": silence,
                "speech_or_sound": speech,
                "human_voice": bool(transcription.get("segments")),
                "music": None,
                "loudness": loudness,
            },
            "transcription": transcription,
            "scenes": scenes,
            "important_moments": [
                {"start": item["start"], "end": item["end"], "score": item["score"]}
                for item in important
            ],
            "vision_used": bool(descriptions),
        }

    async def _silence(self, source: Path, duration: float) -> list[dict[str, float]]:
        _, stderr = await _run_process(
            "ffmpeg",
            "-hide_banner",
            "-nostats",
            "-i",
            str(source),
            "-af",
            "silencedetect=noise=-35dB:d=0.35",
            "-vn",
            "-f",
            "null",
            "-",
            timeout=min(300.0, max(30.0, duration * 2)),
        )
        return _parse_silence(stderr.decode(errors="replace"), duration)

    async def _loudness(self, source: Path) -> dict[str, float | None]:
        _, stderr = await _run_process(
            "ffmpeg",
            "-hide_banner",
            "-nostats",
            "-i",
            str(source),
            "-af",
            "volumedetect",
            "-vn",
            "-f",
            "null",
            "-",
            timeout=300,
        )
        text = stderr.decode(errors="replace")
        mean = _MEAN_VOLUME_RE.search(text)
        maximum = _MAX_VOLUME_RE.search(text)
        return {
            "mean_db": float(mean.group(1)) if mean else None,
            "max_db": float(maximum.group(1)) if maximum else None,
        }

    async def _scenes(
        self, source: Path, duration: float, output_dir: Path
    ) -> list[dict[str, Any]]:
        pattern = output_dir / "scene-%03d.jpg"
        _, stderr = await _run_process(
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "info",
            "-nostats",
            "-i",
            str(source),
            "-vf",
            "select='eq(n,0)+gt(scene,0.32)',showinfo,scale=320:-2",
            "-fps_mode",
            "vfr",
            "-frames:v",
            "12",
            str(pattern),
            timeout=min(300.0, max(30.0, duration * 2)),
        )
        starts = [float(value) for value in _SCENE_RE.findall(stderr.decode(errors="replace"))]
        frames = sorted(output_dir.glob("scene-*.jpg"))
        if not starts and frames:
            starts = [0.0]
        starts = starts[: len(frames)]
        result: list[dict[str, Any]] = []
        for index, start in enumerate(starts):
            end = starts[index + 1] if index + 1 < len(starts) else duration
            scene_duration = max(0.04, end - start)
            result.append(
                {
                    "start": round(start, 3),
                    "end": round(end, 3),
                    "score": round(min(1.0, 0.4 + scene_duration / max(duration, 1) * 0.6), 3),
                    "thumbnail": str(frames[index]),
                }
            )
        return result


media_intelligence_service = MediaIntelligenceService()
