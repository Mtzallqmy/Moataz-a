from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.config import Settings, get_settings
from app.db import MediaProject
from app.services.projects import ProjectAssetItem, ProjectService, project_service


@dataclass(frozen=True, slots=True)
class RenderAsset:
    asset_id: int
    asset_type: str
    role: str
    position: int
    path: Path
    duration: float | None
    width: int | None
    height: int | None
    metadata: dict[str, Any]


@dataclass(frozen=True, slots=True)
class RenderPlan:
    project_id: int
    user_id: int
    template: str
    width: int
    height: int
    fps: int
    aspect_ratio: str
    fit_mode: str
    transition: str
    audio_mode: str
    logo_position: str
    assets: tuple[RenderAsset, ...]
    expected_duration: float


def _metadata(raw: str) -> dict[str, Any]:
    try:
        value = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def _timeline(project: MediaProject) -> dict[str, Any]:
    try:
        value = json.loads(project.timeline_json or "{}")
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def _auto_template(items: list[RenderAsset]) -> str:
    primary = [item for item in items if item.role != "logo"]
    roles = {item.role for item in primary}
    images = [item for item in primary if item.asset_type == "image"]
    audios = [item for item in primary if item.asset_type in {"audio", "voice"}]
    videos = [item for item in primary if item.asset_type == "video"]
    if {"intro", "outro"}.issubset(roles) and videos:
        return "intro_main_outro"
    if len(images) == 1 and len(audios) == 1 and not videos:
        return "audio_image"
    if images and not videos:
        return "slideshow"
    if videos and audios:
        return "video_audio"
    if videos:
        return "merge_videos"
    raise ValueError("Project assets do not match an MVP template")


def _duration_for(template: str, items: list[RenderAsset]) -> float:
    primary = [item for item in items if item.role != "logo"]
    images = [item for item in primary if item.asset_type == "image"]
    audios = [item for item in primary if item.asset_type in {"audio", "voice", "music"}]
    videos = [item for item in primary if item.asset_type == "video"]
    unsupported = [
        item
        for item in primary
        if item.asset_type not in {"image", "video", "audio", "voice"}
    ]
    if unsupported:
        raise ValueError(
            "The selected template cannot render asset types: "
            + ", ".join(sorted({item.asset_type for item in unsupported}))
        )
    if template == "audio_image":
        if len(images) != 1 or len(audios) != 1 or videos or len(primary) != 2:
            raise ValueError("Audio + Image requires one image and one audio asset")
        return float(audios[0].duration or 0)
    if template == "slideshow":
        if not images or videos or len(audios) > 1 or len(primary) != len(images) + len(audios):
            raise ValueError("Slideshow requires images and at most one optional audio asset")
        audio = next((item for item in audios if item.role != "music"), audios[0] if audios else None)
        return float(audio.duration or 0) if audio else float(len(images) * 4)
    if template == "merge_videos":
        if not videos or images or audios or len(primary) != len(videos):
            raise ValueError("Merge Videos requires only video assets (an optional logo is allowed)")
        return sum(float(item.duration or 0) for item in videos)
    if template == "video_audio":
        if len(videos) != 1 or len(audios) != 1 or images or len(primary) != 2:
            raise ValueError("Video + Audio requires exactly one video and one audio asset")
        return float(videos[0].duration or 0)
    if template == "intro_main_outro":
        ordered = [item for item in primary if item.asset_type == "video"]
        role_counts = {role: sum(item.role == role for item in ordered) for role in {"intro", "main", "outro"}}
        if (
            images
            or audios
            or len(primary) != len(ordered)
            or role_counts["intro"] != 1
            or role_counts["outro"] != 1
            or role_counts["main"] < 1
        ):
            raise ValueError("Intro/Main/Outro requires one intro, at least one main, and one outro video")
        return sum(float(item.duration or 0) for item in ordered)
    if template == "logo_overlay":
        logos = [item for item in items if item.role == "logo"]
        if len(videos) != 1 or len(logos) != 1 or images or audios or len(primary) != 1:
            raise ValueError("Logo Overlay requires exactly one video and one logo")
        return float(videos[0].duration or 0)
    raise ValueError("Unsupported renderer template")


class ComposerService:
    """Convert renderer-independent project state into a concrete RenderPlan."""

    def __init__(self, settings: Settings | None = None, *, projects: ProjectService | None = None) -> None:
        self.settings = settings or get_settings()
        self.projects = projects or project_service

    async def build(self, project_id: int, *, user_id: int | None = None) -> RenderPlan:
        project = await self.projects.get_project(project_id, user_id=user_id)
        if project is None:
            raise LookupError("Project not found")
        linked = await self.projects.list_assets(project_id, user_id=user_id)
        if not linked:
            raise ValueError("Project has no assets")
        items = [self._render_asset(item) for item in linked]
        storage_root = self.settings.project_dir.resolve()
        for item in items:
            if storage_root not in item.path.resolve().parents:
                raise ValueError(f"Asset #{item.asset_id} has an unsafe storage path")
            if not item.path.exists() or item.path.stat().st_size <= 0:
                raise FileNotFoundError(f"Asset #{item.asset_id} is missing from storage")
        timeline = _timeline(project)
        options = timeline.get("options") if isinstance(timeline.get("options"), dict) else {}
        template = str(timeline.get("template") or "auto")
        if template == "auto":
            template = _auto_template(items)
        fit_mode = str(options.get("fit_mode") or "fit")
        transition = str(options.get("transition") or "none")
        audio_mode = str(options.get("audio_mode") or "replace_audio")
        logo_position = str(options.get("logo_position") or "top-right")
        if fit_mode not in {"fit", "fill", "blur-background"}:
            raise ValueError("Unsupported fit mode")
        if transition not in {"none", "fade"}:
            raise ValueError("Unsupported transition")
        if audio_mode not in {"replace_audio", "mix_audio", "background_music"}:
            raise ValueError("Unsupported audio mode")
        if logo_position not in {"top-left", "top-right", "bottom-left", "bottom-right", "center"}:
            raise ValueError("Unsupported logo position")
        duration = _duration_for(template, items)
        if duration <= 0:
            raise ValueError("Project duration could not be determined")
        if duration > self.settings.max_project_duration_seconds:
            raise ValueError("Project duration exceeds configured limit")
        if duration > self.settings.max_render_duration_seconds:
            raise ValueError("Render duration exceeds configured limit")
        return RenderPlan(
            project_id=project.id,
            user_id=project.user_id,
            template=template,
            width=project.width,
            height=project.height,
            fps=project.fps,
            aspect_ratio=project.aspect_ratio,
            fit_mode=fit_mode,
            transition=transition,
            audio_mode=audio_mode,
            logo_position=logo_position,
            assets=tuple(items),
            expected_duration=duration,
        )

    @staticmethod
    def _render_asset(item: ProjectAssetItem) -> RenderAsset:
        asset = item.asset
        return RenderAsset(
            asset_id=asset.id,
            asset_type=asset.asset_type,
            role=item.link.role,
            position=item.link.position,
            path=Path(asset.local_path),
            duration=asset.duration,
            width=asset.width,
            height=asset.height,
            metadata=_metadata(asset.metadata_json),
        )


composer_service = ComposerService()
