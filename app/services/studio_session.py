from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from app.services.projects import ProjectAssetItem


class StudioWorkflow(StrEnum):
    SMART_EDIT = "smart_edit"
    CUT = "cut"
    AUDIO = "audio"
    CAPTIONS = "captions"
    REOPEN = "reopen"


class StudioPhase(StrEnum):
    NEW = "NEW"
    COLLECTING_MEDIA = "COLLECTING_MEDIA"
    READY_FOR_INSTRUCTIONS = "READY_FOR_INSTRUCTIONS"
    ANALYZING = "ANALYZING"
    PLANNING = "PLANNING"
    EDITING = "EDITING"
    PREVIEW_RENDERING = "PREVIEW_RENDERING"
    AWAITING_FEEDBACK = "AWAITING_FEEDBACK"
    FINAL_RENDERING = "FINAL_RENDERING"
    COMPLETED = "COMPLETED"


_TRANSITIONS: dict[StudioPhase, set[StudioPhase]] = {
    StudioPhase.NEW: {StudioPhase.COLLECTING_MEDIA},
    StudioPhase.COLLECTING_MEDIA: {StudioPhase.READY_FOR_INSTRUCTIONS},
    StudioPhase.READY_FOR_INSTRUCTIONS: {
        StudioPhase.COLLECTING_MEDIA,
        StudioPhase.ANALYZING,
        StudioPhase.EDITING,
    },
    StudioPhase.ANALYZING: {
        StudioPhase.PLANNING,
        StudioPhase.EDITING,
        StudioPhase.AWAITING_FEEDBACK,
    },
    StudioPhase.PLANNING: {StudioPhase.EDITING, StudioPhase.AWAITING_FEEDBACK},
    StudioPhase.EDITING: {
        StudioPhase.PREVIEW_RENDERING,
        StudioPhase.AWAITING_FEEDBACK,
        StudioPhase.FINAL_RENDERING,
    },
    StudioPhase.PREVIEW_RENDERING: {StudioPhase.AWAITING_FEEDBACK},
    StudioPhase.AWAITING_FEEDBACK: {
        StudioPhase.COLLECTING_MEDIA,
        StudioPhase.ANALYZING,
        StudioPhase.EDITING,
        StudioPhase.PREVIEW_RENDERING,
        StudioPhase.FINAL_RENDERING,
    },
    StudioPhase.FINAL_RENDERING: {StudioPhase.COMPLETED},
    StudioPhase.COMPLETED: {StudioPhase.COLLECTING_MEDIA, StudioPhase.AWAITING_FEEDBACK},
}


def transition_phase(current: str | StudioPhase, target: str | StudioPhase) -> StudioPhase:
    source = StudioPhase(current)
    destination = StudioPhase(target)
    if destination == source:
        return destination
    if destination not in _TRANSITIONS[source]:
        raise ValueError(f"Invalid Studio phase transition: {source} -> {destination}")
    return destination


@dataclass(frozen=True, slots=True)
class ProjectMediaSummary:
    videos: int = 0
    images: int = 0
    music: int = 0
    voice_audio: int = 0
    subtitles: int = 0
    logos: int = 0
    other: int = 0
    video_duration: float = 0.0

    @property
    def total(self) -> int:
        return (
            self.videos
            + self.images
            + self.music
            + self.voice_audio
            + self.subtitles
            + self.logos
            + self.other
        )


def _metadata(item: ProjectAssetItem) -> dict[str, Any]:
    try:
        value = json.loads(item.asset.metadata_json or "{}")
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def asset_display_name(item: ProjectAssetItem) -> str:
    metadata = _metadata(item)
    value = metadata.get("original_name") or metadata.get("title")
    return str(value or Path(item.asset.local_path).name).replace("\n", " ")[:80]


def classify_project_assets(items: Iterable[ProjectAssetItem]) -> ProjectMediaSummary:
    values = {
        "videos": 0,
        "images": 0,
        "music": 0,
        "voice_audio": 0,
        "subtitles": 0,
        "logos": 0,
        "other": 0,
    }
    duration = 0.0
    for item in items:
        kind = item.asset.asset_type
        role = item.link.role
        name = asset_display_name(item).casefold()
        if role == "logo" or kind == "logo" or "logo" in name or "شعار" in name:
            values["logos"] += 1
        elif kind == "video":
            values["videos"] += 1
            duration += float(item.asset.duration or 0)
        elif kind == "image":
            values["images"] += 1
        elif kind == "subtitle":
            values["subtitles"] += 1
        elif kind == "voice" or role == "voice":
            values["voice_audio"] += 1
        elif kind == "audio" and (
            role in {"music", "background"}
            or any(token in name for token in ("music", "song", "track", "موسيقى"))
        ):
            values["music"] += 1
        elif kind == "audio":
            values["voice_audio"] += 1
        else:
            values["other"] += 1
    return ProjectMediaSummary(**values, video_duration=duration)


def timeline_duration(timeline: dict[str, Any]) -> float:
    ends = [
        float(clip.get("start") or 0) + float(clip.get("duration") or 0)
        for track in timeline.get("tracks") or []
        if isinstance(track, dict)
        for clip in track.get("clips") or []
        if isinstance(clip, dict)
    ]
    return max(ends, default=0.0)


def _short_time(seconds: float) -> str:
    total = max(0, int(round(seconds)))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    return (
        f"{hours:02d}:{minutes:02d}:{secs:02d}"
        if hours
        else f"{minutes:02d}:{secs:02d}"
    )


def recent_asset_context(
    items: Iterable[ProjectAssetItem], recent_asset_id: int | None
) -> dict[str, Any] | None:
    sequence = list(items)
    selected = next(
        (item for item in sequence if item.asset.id == recent_asset_id), None
    )
    if selected is None:
        return None
    same_type = [item for item in sequence if item.asset.asset_type == selected.asset.asset_type]
    return {
        "asset_id": selected.asset.id,
        "asset_type": selected.asset.asset_type,
        "role": selected.link.role,
        "name": asset_display_name(selected),
        "type_index": same_type.index(selected) + 1,
        "ambiguous_type_count": len(same_type),
    }


def project_summary_text(
    project: Any,
    items: Iterable[ProjectAssetItem],
    timeline: dict[str, Any],
    *,
    phase: str | StudioPhase,
) -> str:
    summary = classify_project_assets(items)
    options = timeline.get("options") or {}
    captions = "مفعّل" if any(
        track.get("kind") == "subtitle" and track.get("clips")
        for track in timeline.get("tracks") or []
        if isinstance(track, dict)
    ) else "غير مفعّل"
    return (
        f"📋 ملخص Project #{project.id}\n"
        f"الحالة: {StudioPhase(phase).value}\n\n"
        f"🎥 فيديوهات: {summary.videos}  🖼 صور: {summary.images}\n"
        f"🎵 موسيقى: {summary.music}  🎙 أصوات: {summary.voice_audio}\n"
        f"🏷 شعارات: {summary.logos}  💬 ترجمة: {summary.subtitles}\n"
        f"مدة الفيديوهات: {_short_time(summary.video_duration)}\n"
        f"Timeline: 00:00 → {_short_time(timeline_duration(timeline))}\n"
        f"Style: {options.get('style_preset', 'غير محدد')}\n"
        f"Canvas: {project.aspect_ratio}\n"
        f"Captions: {captions}\n"
        f"Audio: {options.get('audio_mode', 'mix_audio')}\n"
        f"Revision: {int(timeline.get('revision') or 0)}"
    )
