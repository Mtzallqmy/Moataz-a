from __future__ import annotations

import copy
import json
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select

from app.config import Settings, get_settings
from app.db import (
    MediaAsset,
    MediaProject,
    ProjectAsset,
    ProjectStatus,
    SessionLocal,
    TimelineRevision,
)
from app.services.project_locks import project_mutation_lock

TRANSITIONS = {
    "none",
    "fade",
    "dissolve",
    "slide",
    "wipe",
    "zoom",
    "blur",
    "push",
    "dip-to-black",
}
FIT_MODES = {"fit", "fill", "blur-background"}
AUDIO_MODES = {"replace_audio", "mix_audio", "background_music"}
TRACK_KINDS = {"visual", "audio", "overlay", "text", "subtitle"}
TOOL_NAMES = {
    "list_assets",
    "inspect_asset",
    "add_clip",
    "remove_clip",
    "trim_clip",
    "split_clip",
    "move_clip",
    "reorder_clips",
    "set_duration",
    "set_speed",
    "add_transition",
    "add_text",
    "add_overlay",
    "set_volume",
    "add_background_music",
    "set_audio_mode",
    "set_canvas",
    "set_fit_mode",
    "set_keyframes",
    "set_transform",
    "set_fades",
    "add_subtitles",
    "undo",
    "redo",
    "render_preview",
    "render_final",
}


@dataclass(frozen=True, slots=True)
class TimelineToolResult:
    timeline: dict[str, Any]
    messages: tuple[str, ...]
    render_action: str | None = None


def _identifier(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def new_timeline(width: int, height: int, fps: int) -> dict[str, Any]:
    return {
        "version": 2,
        "revision": 0,
        "canvas": {
            "width": int(width),
            "height": int(height),
            "fps": int(fps),
            "background": "black",
        },
        "template": "timeline",
        "options": {
            "fit_mode": "fit",
            "audio_mode": "mix_audio",
            "transition": "none",
            "logo_position": "top-right",
        },
        "tracks": [
            {"id": "visual-main", "kind": "visual", "clips": []},
            {"id": "audio-main", "kind": "audio", "clips": []},
            {"id": "overlay-main", "kind": "overlay", "clips": []},
            {"id": "text-main", "kind": "text", "clips": []},
            {"id": "subtitle-main", "kind": "subtitle", "clips": []},
        ],
        "history": {"undo": [], "redo": []},
    }


def parse_timeline(raw: str, *, width: int, height: int, fps: int) -> dict[str, Any]:
    try:
        value = json.loads(raw or "{}")
    except json.JSONDecodeError:
        value = {}
    if not isinstance(value, dict) or value.get("version") != 2:
        migrated = new_timeline(width, height, fps)
        if isinstance(value, dict):
            migrated["options"].update(value.get("options") or {})
            migrated["template"] = value.get("template") or "timeline"
        return migrated
    return value


def _track(timeline: dict[str, Any], kind: str) -> dict[str, Any]:
    tracks = timeline.setdefault("tracks", [])
    for item in tracks:
        if isinstance(item, dict) and item.get("kind") == kind:
            item.setdefault("clips", [])
            return item
    item = {"id": f"{kind}-main", "kind": kind, "clips": []}
    tracks.append(item)
    return item


def _all_clips(timeline: dict[str, Any]):
    for track in timeline.get("tracks") or []:
        if not isinstance(track, dict):
            continue
        for clip in track.get("clips") or []:
            if isinstance(clip, dict):
                yield track, clip


def _find_clip(timeline: dict[str, Any], clip_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    for track, clip in _all_clips(timeline):
        if clip.get("id") == clip_id:
            return track, clip
    raise ValueError(f"Unknown clip: {clip_id}")


def _number(value: object, name: str, *, minimum: float = 0, maximum: float = 86_400) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a number")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a number") from exc
    if result < minimum or result > maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return result


def _integer(value: object, name: str, *, minimum: int, maximum: int) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be an integer")
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if result < minimum or result > maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return result


def _text(value: object, name: str, *, maximum: int = 500) -> str:
    result = str(value or "").strip()
    if not result:
        raise ValueError(f"{name} cannot be empty")
    if len(result) > maximum:
        raise ValueError(f"{name} is too long")
    return result


def _asset_clip(asset: MediaAsset, *, role: str, position: int) -> tuple[str, dict[str, Any]]:
    duration = float(asset.duration or 4.0)
    base = {
        "id": f"asset-{asset.id}",
        "asset_id": asset.id,
        "asset_type": asset.asset_type,
        "start": 0.0,
        "source_start": 0.0,
        "duration": duration,
        "speed": 1.0,
        "volume": 1.0,
        "fade_in": 0.0,
        "fade_out": 0.0,
        "fit_mode": "fit",
        "position": position,
        "role": role,
        "keyframes": [],
        "transition_out": {"type": "none", "duration": 0.0},
    }
    if role == "logo" or asset.asset_type == "logo":
        base.update({"start": 0.0, "position_name": "top-right", "scale": 0.2})
        return "overlay", base
    if asset.asset_type in {"audio", "voice"}:
        return "audio", base
    if asset.asset_type == "subtitle":
        return "subtitle", base
    return "visual", base


def sync_assets(
    timeline: dict[str, Any],
    linked: list[tuple[ProjectAsset, MediaAsset]],
) -> dict[str, Any]:
    """Synchronize project membership without destroying user-authored edits."""
    result = copy.deepcopy(timeline)
    allowed = {asset.id for _, asset in linked}
    for track in result.get("tracks") or []:
        if isinstance(track, dict):
            track["clips"] = [
                clip
                for clip in track.get("clips") or []
                if not isinstance(clip, dict)
                or clip.get("asset_id") is None
                or clip.get("asset_id") in allowed
            ]
    present = {
        int(clip["asset_id"])
        for _, clip in _all_clips(result)
        if isinstance(clip.get("asset_id"), int)
    }
    for link, asset in linked:
        initial_id = f"asset-{asset.id}"
        existing = next(
            ((track, clip) for track, clip in _all_clips(result) if clip.get("id") == initial_id),
            None,
        )
        if existing is not None:
            current_track, clip = existing
            desired_kind, _ = _asset_clip(asset, role=link.role, position=link.position)
            clip["role"] = link.role
            clip["asset_type"] = asset.asset_type
            if current_track.get("kind") != desired_kind:
                current_track["clips"].remove(clip)
                _track(result, desired_kind)["clips"].append(clip)
            continue
        if asset.id in present:
            continue
        kind, clip = _asset_clip(asset, role=link.role, position=link.position)
        _track(result, kind)["clips"].append(clip)
    _reflow_visuals(result)
    validate_timeline(result, max_clips=1000)
    return result


def _reflow_visuals(timeline: dict[str, Any]) -> None:
    cursor = 0.0
    clips = _track(timeline, "visual")["clips"]
    clips.sort(key=lambda item: (int(item.get("position", 0)), str(item.get("id", ""))))
    for position, clip in enumerate(clips):
        clip["position"] = position
        clip["start"] = round(cursor, 6)
        transition = clip.get("transition_out") or {}
        overlap = float(transition.get("duration") or 0) if transition.get("type") != "none" else 0.0
        cursor += float(clip.get("duration") or 0) - overlap


def validate_timeline(timeline: dict[str, Any], *, max_clips: int = 200) -> None:
    if timeline.get("version") != 2:
        raise ValueError("Timeline version 2 is required")
    canvas = timeline.get("canvas")
    if not isinstance(canvas, dict):
        raise ValueError("Timeline canvas is missing")
    width = _integer(canvas.get("width"), "canvas width", minimum=64, maximum=7680)
    height = _integer(canvas.get("height"), "canvas height", minimum=64, maximum=7680)
    if width % 2 or height % 2:
        raise ValueError("Canvas dimensions must be even")
    _integer(canvas.get("fps"), "canvas fps", minimum=1, maximum=120)
    tracks = timeline.get("tracks")
    if not isinstance(tracks, list):
        raise ValueError("Timeline tracks must be a list")
    seen_tracks: set[str] = set()
    seen_clips: set[str] = set()
    count = 0
    for track in tracks:
        if not isinstance(track, dict) or track.get("kind") not in TRACK_KINDS:
            raise ValueError("Timeline contains an invalid track")
        track_id = _text(track.get("id"), "track id", maximum=80)
        if track_id in seen_tracks:
            raise ValueError("Timeline track IDs must be unique")
        seen_tracks.add(track_id)
        clips = track.get("clips")
        if not isinstance(clips, list):
            raise ValueError("Timeline clips must be a list")
        for clip in clips:
            count += 1
            if count > max_clips:
                raise ValueError("Timeline clip limit exceeded")
            if not isinstance(clip, dict):
                raise ValueError("Timeline clip must be an object")
            clip_id = _text(clip.get("id"), "clip id", maximum=80)
            if clip_id in seen_clips:
                raise ValueError("Timeline clip IDs must be unique")
            seen_clips.add(clip_id)
            _number(clip.get("start", 0), "clip start")
            _number(clip.get("duration", 0), "clip duration", minimum=0.04)
            _number(clip.get("speed", 1), "clip speed", minimum=0.25, maximum=4)
            _number(clip.get("volume", 1), "clip volume", maximum=2)
            fit = clip.get("fit_mode", "fit")
            if fit not in FIT_MODES:
                raise ValueError("Unknown clip fit mode")
            transition = clip.get("transition_out") or {}
            if transition.get("type", "none") not in TRANSITIONS:
                raise ValueError("Unknown transition")
            _number(transition.get("duration", 0), "transition duration", maximum=10)
            keyframes = clip.get("keyframes") or []
            if not isinstance(keyframes, list) or len(keyframes) > 50:
                raise ValueError("Invalid keyframes")


class TimelineService:
    """Validated, renderer-neutral command layer shared by Telegram and AI."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    async def get(self, project_id: int, *, user_id: int) -> dict[str, Any]:
        async with SessionLocal() as session:
            project = await session.scalar(
                select(MediaProject).where(
                    MediaProject.id == project_id, MediaProject.user_id == user_id
                )
            )
            if project is None:
                raise LookupError("Project not found")
            timeline = parse_timeline(
                project.timeline_json, width=project.width, height=project.height, fps=project.fps
            )
            linked = list(
                (
                    await session.execute(
                        select(ProjectAsset, MediaAsset)
                        .join(MediaAsset, MediaAsset.id == ProjectAsset.asset_id)
                        .where(ProjectAsset.project_id == project_id)
                        .order_by(ProjectAsset.position)
                    )
                ).all()
            )
            return sync_assets(timeline, linked)

    async def apply(
        self,
        project_id: int,
        *,
        user_id: int,
        calls: list[dict[str, Any]],
    ) -> TimelineToolResult:
        if not calls or len(calls) > 20:
            raise ValueError("Agent must provide between 1 and 20 tool calls")
        async with project_mutation_lock(project_id):
            return await self._apply_locked(
                project_id, user_id=user_id, calls=calls
            )

    async def _apply_locked(
        self,
        project_id: int,
        *,
        user_id: int,
        calls: list[dict[str, Any]],
    ) -> TimelineToolResult:
        async with SessionLocal() as session:
            project = await session.scalar(
                select(MediaProject)
                .where(
                    MediaProject.id == project_id, MediaProject.user_id == user_id
                )
                .with_for_update()
            )
            if project is None:
                raise LookupError("Project not found")
            if project.status == ProjectStatus.CANCELLED.value:
                raise ValueError("Cancelled project cannot be edited")
            rows = list(
                (
                    await session.execute(
                        select(ProjectAsset, MediaAsset)
                        .join(MediaAsset, MediaAsset.id == ProjectAsset.asset_id)
                        .where(ProjectAsset.project_id == project_id)
                        .order_by(ProjectAsset.position)
                    )
                ).all()
            )
            assets = {asset.id: asset for _, asset in rows}
            timeline = sync_assets(
                parse_timeline(
                    project.timeline_json,
                    width=project.width,
                    height=project.height,
                    fps=project.fps,
                ),
                rows,
            )
            await self._ensure_initial_revision(session, project, timeline)
            messages: list[str] = []
            render_action: str | None = None
            mutated = False
            normalized_calls: list[dict[str, Any]] = []
            for raw_call in calls:
                if not isinstance(raw_call, dict):
                    raise ValueError("Each tool call must be an object")
                name = str(raw_call.get("name") or "")
                raw_args = raw_call.get("arguments") or {}
                if name not in TOOL_NAMES or not isinstance(raw_args, dict):
                    raise ValueError(f"Unknown or invalid agent tool: {name}")
                args = dict(raw_args)
                if name == "undo":
                    if len(calls) != 1:
                        raise ValueError("undo must be the only tool call")
                    return await self._undo(session, project, timeline)
                if name == "redo":
                    if len(calls) != 1:
                        raise ValueError("redo must be the only tool call")
                    return await self._redo(session, project, timeline)
                if name in {"render_preview", "render_final"}:
                    render_action = "preview" if name == "render_preview" else "final"
                    messages.append("Preview requested" if render_action == "preview" else "Final render requested")
                else:
                    self._resolve_clip_reference(timeline, name, args)
                    changed, message = self._execute(timeline, name, args, assets)
                    mutated = mutated or changed
                    messages.append(message)
                normalized_calls.append({"name": name, "arguments": args})
            validate_timeline(timeline, max_clips=self.settings.max_project_assets * 8)
            if mutated:
                timeline["template"] = "timeline"
                await self._save_revision(
                    session,
                    project,
                    timeline,
                    {"name": "agent_batch", "calls": normalized_calls},
                )
                project.status = ProjectStatus.READY.value
                await session.commit()
            return TimelineToolResult(timeline, tuple(messages), render_action)

    @staticmethod
    def _resolve_clip_reference(
        timeline: dict[str, Any], name: str, args: dict[str, Any]
    ) -> None:
        target_kinds = {
            "remove_clip": TRACK_KINDS,
            "trim_clip": {"visual", "audio", "overlay"},
            "split_clip": {"visual", "audio", "overlay"},
            "move_clip": TRACK_KINDS,
            "reorder_clips": TRACK_KINDS,
            "set_duration": TRACK_KINDS,
            "set_speed": {"visual", "audio", "overlay"},
            "set_volume": {"audio", "visual"},
            "set_fades": TRACK_KINDS,
            "set_transform": {"visual", "overlay"},
            "add_transition": {"visual"},
            "set_keyframes": TRACK_KINDS,
            "set_fit_mode": {"visual", "overlay"},
        }.get(name)
        if target_kinds is None or args.get("clip_id"):
            return
        if name == "set_fit_mode" and args.get("apply_to_all"):
            return

        candidates = [
            clip
            for track, clip in _all_clips(timeline)
            if track.get("kind") in target_kinds
        ]
        if args.get("asset_id") is not None:
            asset_id = _integer(
                args.get("asset_id"), "asset_id", minimum=1, maximum=2**31 - 1
            )
            candidates = [clip for clip in candidates if clip.get("asset_id") == asset_id]
        if args.get("clip_index") is not None:
            index = _integer(
                args.get("clip_index"),
                "clip_index",
                minimum=1,
                maximum=max(1, len(candidates)),
            )
            if index > len(candidates):
                raise ValueError("clip_index does not exist in the current timeline")
            candidates = [candidates[index - 1]]
        if len(candidates) == 1:
            args["clip_id"] = str(candidates[0]["id"])
            return

        available = ", ".join(str(clip.get("id")) for clip in candidates[:12])
        if not candidates:
            raise ValueError(f"{name} requires a media clip, but none is available")
        raise ValueError(
            f"{name} requires clip_id, asset_id, or 1-based clip_index; "
            f"available clip IDs: {available}"
        )

    async def undo(self, project_id: int, *, user_id: int) -> TimelineToolResult:
        return await self.apply(project_id, user_id=user_id, calls=[{"name": "undo"}])

    async def redo(self, project_id: int, *, user_id: int) -> TimelineToolResult:
        return await self.apply(project_id, user_id=user_id, calls=[{"name": "redo"}])

    async def _ensure_initial_revision(self, session, project: MediaProject, timeline: dict[str, Any]) -> None:
        exists = await session.scalar(
            select(TimelineRevision.id).where(TimelineRevision.project_id == project.id).limit(1)
        )
        if exists is None:
            timeline["revision"] = 0
            session.add(
                TimelineRevision(
                    project_id=project.id,
                    revision=0,
                    timeline_json=json.dumps(timeline, ensure_ascii=False, separators=(",", ":")),
                    operation_json='{"name":"initial"}',
                )
            )
            await session.flush()

    async def _save_revision(
        self,
        session,
        project: MediaProject,
        timeline: dict[str, Any],
        operation: dict[str, Any],
        *,
        track_history: bool = True,
    ) -> None:
        maximum = int(
            await session.scalar(
                select(func.max(TimelineRevision.revision)).where(
                    TimelineRevision.project_id == project.id
                )
            )
            or 0
        )
        current = int(timeline.get("revision", 0))
        if track_history:
            history = timeline.setdefault("history", {"undo": [], "redo": []})
            history["undo"] = [*list(history.get("undo") or []), current][-100:]
            history["redo"] = []
        revision = maximum + 1
        timeline["revision"] = revision
        encoded = json.dumps(timeline, ensure_ascii=False, separators=(",", ":"))
        project.timeline_json = encoded
        canvas = timeline["canvas"]
        project.width = int(canvas["width"])
        project.height = int(canvas["height"])
        project.fps = int(canvas["fps"])
        project.aspect_ratio = f"{project.width}:{project.height}"
        session.add(
            TimelineRevision(
                project_id=project.id,
                revision=revision,
                timeline_json=encoded,
                operation_json=json.dumps(operation, ensure_ascii=False, separators=(",", ":")),
            )
        )

    async def _undo(self, session, project: MediaProject, timeline: dict[str, Any]) -> TimelineToolResult:
        history = timeline.setdefault("history", {"undo": [], "redo": []})
        undo = list(history.get("undo") or [])
        if not undo:
            raise ValueError("Nothing to undo")
        target_revision = int(undo.pop())
        target = await session.scalar(
            select(TimelineRevision).where(
                TimelineRevision.project_id == project.id,
                TimelineRevision.revision == target_revision,
            )
        )
        if target is None:
            raise ValueError("Undo revision is unavailable")
        restored = json.loads(target.timeline_json)
        restored["history"] = {
            "undo": undo,
            "redo": [*list(history.get("redo") or []), int(timeline.get("revision", 0))][-100:],
        }
        await self._save_revision(
            session,
            project,
            restored,
            {"name": "undo", "target": target_revision},
            track_history=False,
        )
        project.status = ProjectStatus.READY.value
        await session.commit()
        return TimelineToolResult(restored, ("Undone",))

    async def _redo(self, session, project: MediaProject, timeline: dict[str, Any]) -> TimelineToolResult:
        history = timeline.setdefault("history", {"undo": [], "redo": []})
        redo = list(history.get("redo") or [])
        if not redo:
            raise ValueError("Nothing to redo")
        target_revision = int(redo.pop())
        target = await session.scalar(
            select(TimelineRevision).where(
                TimelineRevision.project_id == project.id,
                TimelineRevision.revision == target_revision,
            )
        )
        if target is None:
            raise ValueError("Redo revision is unavailable")
        restored = json.loads(target.timeline_json)
        restored["history"] = {
            "undo": [*list(history.get("undo") or []), int(timeline.get("revision", 0))],
            "redo": redo,
        }
        await self._save_revision(
            session,
            project,
            restored,
            {"name": "redo", "target": target_revision},
            track_history=False,
        )
        project.status = ProjectStatus.READY.value
        await session.commit()
        return TimelineToolResult(restored, ("Redone",))

    def _execute(
        self,
        timeline: dict[str, Any],
        name: str,
        args: dict[str, Any],
        assets: dict[int, MediaAsset],
    ) -> tuple[bool, str]:
        if name == "list_assets":
            return False, ", ".join(f"#{asset.id}:{asset.asset_type}" for asset in assets.values()) or "No assets"
        if name == "inspect_asset":
            asset_id = _integer(args.get("asset_id"), "asset_id", minimum=1, maximum=2**31 - 1)
            asset = assets.get(asset_id)
            if asset is None:
                raise ValueError("Asset is not attached to this project")
            return False, f"Asset #{asset.id}: {asset.asset_type}, duration={asset.duration or 0:.2f}s, {asset.width or 0}x{asset.height or 0}"
        if name in {"add_clip", "add_background_music", "add_overlay"}:
            asset_id = _integer(args.get("asset_id"), "asset_id", minimum=1, maximum=2**31 - 1)
            asset = assets.get(asset_id)
            if asset is None:
                raise ValueError("Asset is not attached to this project")
            if name == "add_background_music" and asset.asset_type not in {"audio", "voice"}:
                raise ValueError("Background music requires an audio asset")
            if name == "add_overlay" and asset.asset_type not in {"image", "logo", "video"}:
                raise ValueError("Overlay requires an image, logo, or video asset")
            requested_kind = str(args.get("track") or "")
            default_kind, clip = _asset_clip(asset, role="music" if name == "add_background_music" else "main", position=9999)
            kind = {"add_background_music": "audio", "add_overlay": "overlay"}.get(name, requested_kind or default_kind)
            if kind not in TRACK_KINDS:
                raise ValueError("Unknown track kind")
            clip["id"] = _identifier("clip")
            clip["start"] = _number(args.get("start", 0), "start")
            if args.get("duration") is not None:
                clip["duration"] = _number(args["duration"], "duration", minimum=0.04)
            if name == "add_background_music":
                clip["volume"] = _number(args.get("volume", 0.2), "volume", maximum=2)
                clip["role"] = "music"
            if name == "add_overlay":
                clip["position_name"] = str(args.get("position") or "top-right")
                clip["scale"] = _number(args.get("scale", 0.2), "scale", minimum=0.02, maximum=1)
            _track(timeline, kind)["clips"].append(clip)
            if kind == "visual":
                _reflow_visuals(timeline)
            return True, f"Added {clip['id']}"
        if name == "add_text":
            clip = {
                "id": _identifier("text"),
                "text": _text(args.get("text"), "text", maximum=500),
                "start": _number(args.get("start", 0), "start"),
                "duration": _number(args.get("duration", 3), "duration", minimum=0.04),
                "speed": 1.0,
                "volume": 0.0,
                "fit_mode": "fit",
                "position_name": str(args.get("position") or "center"),
                "font_size": _integer(args.get("font_size", 48), "font_size", minimum=12, maximum=240),
                "color": str(args.get("color") or "white")[:32],
                "fade_in": _number(args.get("fade_in", 0), "fade_in", maximum=10),
                "fade_out": _number(args.get("fade_out", 0), "fade_out", maximum=10),
                "keyframes": [],
                "transition_out": {"type": "none", "duration": 0.0},
            }
            _track(timeline, "text")["clips"].append(clip)
            return True, f"Added {clip['id']}"
        if name == "add_subtitles":
            cues = args.get("cues")
            if not isinstance(cues, list) or not cues or len(cues) > 200:
                raise ValueError("Subtitles require 1-200 cues")
            clips = _track(timeline, "subtitle")["clips"]
            for cue in cues:
                if not isinstance(cue, dict):
                    raise ValueError("Subtitle cue must be an object")
                start = _number(cue.get("start"), "subtitle start")
                end = _number(cue.get("end"), "subtitle end", minimum=start + 0.04)
                clips.append(
                    {
                        "id": _identifier("caption"),
                        "text": _text(cue.get("text"), "subtitle text", maximum=500),
                        "start": start,
                        "duration": end - start,
                        "speed": 1.0,
                        "volume": 0.0,
                        "fit_mode": "fit",
                        "position_name": "bottom",
                        "font_size": 42,
                        "color": "white",
                        "keyframes": [],
                        "transition_out": {"type": "none", "duration": 0.0},
                    }
                )
            return True, f"Added {len(cues)} subtitle cues"
        if name == "set_audio_mode":
            mode = str(args.get("mode") or "")
            if mode not in AUDIO_MODES:
                raise ValueError("Unsupported audio mode")
            timeline.setdefault("options", {})["audio_mode"] = mode
            return True, f"Audio mode set to {mode}"
        if name == "set_canvas":
            preset = str(args.get("preset") or "")
            presets = {
                "9:16": (1080, 1920),
                "vertical": (1080, 1920),
                "16:9": (1920, 1080),
                "horizontal": (1920, 1080),
                "1:1": (1080, 1080),
                "square": (1080, 1080),
            }
            if preset in presets:
                width, height = presets[preset]
            else:
                width = _integer(args.get("width"), "width", minimum=64, maximum=7680)
                height = _integer(args.get("height"), "height", minimum=64, maximum=7680)
            if width % 2 or height % 2:
                raise ValueError("Canvas dimensions must be even")
            timeline["canvas"].update({"width": width, "height": height})
            return True, f"Canvas set to {width}x{height}"
        if name == "set_fit_mode" and not args.get("clip_id"):
            mode = str(args.get("mode") or "")
            if mode not in FIT_MODES:
                raise ValueError("Unsupported fit mode")
            for visual_track, item in _all_clips(timeline):
                if visual_track.get("kind") in {"visual", "overlay"}:
                    item["fit_mode"] = mode
            timeline.setdefault("options", {})["fit_mode"] = mode
            return True, f"Fit mode set to {mode}"
        clip_id = _text(args.get("clip_id"), "clip_id", maximum=80)
        track, clip = _find_clip(timeline, clip_id)
        if name == "remove_clip":
            track["clips"].remove(clip)
            if track.get("kind") == "visual":
                _reflow_visuals(timeline)
            return True, f"Removed {clip_id}"
        if name == "trim_clip":
            source_start = _number(args.get("source_start", 0), "source_start")
            source_end = _number(args.get("source_end"), "source_end", minimum=source_start + 0.04)
            asset = assets.get(int(clip.get("asset_id") or 0))
            if asset and asset.duration and source_end > asset.duration + 0.01:
                raise ValueError("Trim exceeds source duration")
            clip["source_start"] = source_start
            clip["duration"] = (source_end - source_start) / float(clip.get("speed", 1))
        elif name == "split_clip":
            at = _number(args.get("at"), "at", minimum=0.04)
            duration = float(clip["duration"])
            if at >= duration - 0.04:
                raise ValueError("Split point must be inside the clip")
            second = copy.deepcopy(clip)
            second["id"] = _identifier("clip")
            second["source_start"] = float(clip.get("source_start", 0)) + at * float(clip.get("speed", 1))
            second["duration"] = duration - at
            clip["duration"] = at
            index = track["clips"].index(clip)
            track["clips"].insert(index + 1, second)
        elif name == "move_clip":
            clip["start"] = _number(args.get("start"), "start")
            if track.get("kind") == "visual":
                track["clips"].sort(key=lambda item: float(item.get("start") or 0))
                for index, item in enumerate(track["clips"]):
                    item["position"] = index
        elif name == "reorder_clips":
            order = args.get("order")
            if not isinstance(order, list) or set(map(str, order)) != {str(item.get("id")) for item in track["clips"]}:
                raise ValueError("order must contain every clip ID in the track exactly once")
            lookup = {str(item["id"]): item for item in track["clips"]}
            track["clips"] = [lookup[str(item)] for item in order]
            for index, item in enumerate(track["clips"]):
                item["position"] = index
        elif name == "set_duration":
            duration = _number(args.get("duration"), "duration", minimum=0.04)
            asset = assets.get(int(clip.get("asset_id") or 0))
            available = float(asset.duration or duration) if asset else duration
            if clip.get("asset_type") == "video" and duration * float(clip.get("speed", 1)) > available + 0.01:
                raise ValueError("Duration exceeds source media")
            clip["duration"] = duration
        elif name == "set_speed":
            speed = _number(args.get("speed"), "speed", minimum=0.25, maximum=4)
            old = float(clip.get("speed", 1))
            clip["speed"] = speed
            clip["duration"] = float(clip["duration"]) * old / speed
        elif name == "set_volume":
            clip["volume"] = _number(args.get("volume"), "volume", maximum=2)
        elif name == "add_transition":
            transition = str(args.get("type") or "none")
            if transition not in TRANSITIONS:
                raise ValueError("Unsupported transition")
            duration = _number(args.get("duration", 0.5), "duration", maximum=10)
            if duration >= float(clip["duration"]):
                raise ValueError("Transition must be shorter than the clip")
            clip["transition_out"] = {"type": transition, "duration": duration}
        elif name == "set_keyframes":
            keyframes = args.get("keyframes")
            if not isinstance(keyframes, list) or len(keyframes) > 50:
                raise ValueError("keyframes must be a list of at most 50 items")
            cleaned = []
            for item in keyframes:
                if not isinstance(item, dict):
                    raise ValueError("keyframe must be an object")
                prop = str(item.get("property") or "")
                if prop not in {"x", "y", "scale", "opacity", "volume"}:
                    raise ValueError("Unsupported keyframe property")
                cleaned.append(
                    {
                        "time": _number(item.get("time"), "keyframe time", maximum=float(clip["duration"])),
                        "property": prop,
                        "value": _number(item.get("value"), "keyframe value", minimum=-10, maximum=10),
                    }
                )
            clip["keyframes"] = cleaned
        elif name == "set_transform":
            if args.get("scale") is not None:
                clip["scale"] = _number(args.get("scale"), "scale", minimum=0.02, maximum=4)
            if args.get("x") is not None:
                clip["x"] = _number(args.get("x"), "x", minimum=-2, maximum=2)
            if args.get("y") is not None:
                clip["y"] = _number(args.get("y"), "y", minimum=-2, maximum=2)
            crop = args.get("crop")
            if crop is not None:
                if not isinstance(crop, dict):
                    raise ValueError("crop must be an object")
                clip["crop"] = {
                    key: _number(crop.get(key, 0), f"crop {key}", maximum=1)
                    for key in ("left", "top", "right", "bottom")
                }
                if clip["crop"]["left"] + clip["crop"]["right"] >= 1:
                    raise ValueError("Horizontal crop removes the whole frame")
                if clip["crop"]["top"] + clip["crop"]["bottom"] >= 1:
                    raise ValueError("Vertical crop removes the whole frame")
        elif name == "set_fades":
            clip["fade_in"] = _number(
                args.get("fade_in", clip.get("fade_in", 0)), "fade_in", maximum=10
            )
            clip["fade_out"] = _number(
                args.get("fade_out", clip.get("fade_out", 0)), "fade_out", maximum=10
            )
        elif name == "set_fit_mode":
            mode = str(args.get("mode") or "")
            if mode not in FIT_MODES:
                raise ValueError("Unsupported fit mode")
            if args.get("apply_to_all"):
                for visual_track, item in _all_clips(timeline):
                    if visual_track.get("kind") in {"visual", "overlay"}:
                        item["fit_mode"] = mode
            else:
                clip["fit_mode"] = mode
            timeline.setdefault("options", {})["fit_mode"] = mode
        else:
            raise ValueError(f"Tool is not implemented: {name}")
        if track.get("kind") == "visual":
            _reflow_visuals(timeline)
        return True, f"Applied {name} to {clip_id}"


timeline_service = TimelineService()
