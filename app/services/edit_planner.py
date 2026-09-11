from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from app.services.ai_registry import AIProviderRegistry, get_ai_provider_registry
from app.services.editing_styles import STYLE_PRESETS
from app.services.openai_compatible import AIProviderError
from app.services.timeline import TRANSITIONS, TimelineService, TimelineToolResult, timeline_service

_ASPECTS = {"9:16", "16:9", "1:1"}
_PACING = {"slow", "medium", "medium-fast", "fast"}
_AUDIO = {"keep", "mute", "replace", "mix", "background_music"}


@dataclass(frozen=True, slots=True)
class EditPlan:
    goal: str
    target_duration: float | None
    aspect_ratio: str
    style_preset: str
    pacing: str
    selected_ranges: tuple[dict[str, Any], ...]
    removed_clip_ids: tuple[str, ...]
    transition: str
    captions: dict[str, Any]
    audio: dict[str, Any]
    texts: tuple[dict[str, Any], ...]
    overlays: tuple[dict[str, Any], ...]
    rationale: str

    @classmethod
    def from_payload(cls, payload: object) -> EditPlan:
        if not isinstance(payload, dict):
            raise ValueError("Edit plan must be an object")
        goal = str(payload.get("goal") or "").strip()
        if not goal or len(goal) > 1000:
            raise ValueError("Edit plan goal is missing or too long")
        raw_duration = payload.get("target_duration")
        duration = None if raw_duration is None else float(raw_duration)
        if duration is not None and not 1 <= duration <= 3600:
            raise ValueError("Edit plan duration must be between 1 and 3600 seconds")
        aspect = str(payload.get("aspect_ratio") or "9:16")
        if aspect not in _ASPECTS:
            raise ValueError("Edit plan has an unsupported aspect ratio")
        style_name = str(payload.get("style_preset") or "minimal")
        if style_name not in STYLE_PRESETS:
            raise ValueError("Edit plan has an unsupported style")
        pacing = str(payload.get("pacing") or STYLE_PRESETS[style_name]["pacing"])
        if pacing not in _PACING:
            raise ValueError("Edit plan has unsupported pacing")
        selected = payload.get("selected_ranges") or []
        if not isinstance(selected, list) or len(selected) > 40:
            raise ValueError("Edit plan selected ranges are invalid")
        cleaned_ranges: list[dict[str, Any]] = []
        for item in selected:
            if not isinstance(item, dict):
                raise ValueError("Selected range must be an object")
            asset_id = int(item.get("asset_id") or 0)
            start = float(item.get("start") or 0)
            end = float(item.get("end") or 0)
            if asset_id <= 0 or start < 0 or end - start < 0.04:
                raise ValueError("Selected range has invalid asset or timestamps")
            cleaned_ranges.append({"asset_id": asset_id, "start": start, "end": end})
        removed = payload.get("removed_clip_ids") or []
        if not isinstance(removed, list) or len(removed) > 40:
            raise ValueError("Removed clips are invalid")
        removed_ids = tuple(str(value).strip() for value in removed)
        if any(not value for value in removed_ids):
            raise ValueError("Removed clip ID cannot be empty")
        transition = str(payload.get("transition") or STYLE_PRESETS[style_name]["transition"])
        if transition not in TRANSITIONS:
            raise ValueError("Edit plan transition is invalid")
        captions = payload.get("captions") or {"enabled": False}
        audio = payload.get("audio") or {"mode": "keep"}
        texts = payload.get("texts") or []
        overlays = payload.get("overlays") or []
        if not isinstance(captions, dict) or not isinstance(audio, dict):
            raise ValueError("Edit plan caption/audio sections are invalid")
        if str(audio.get("mode") or "keep") not in _AUDIO:
            raise ValueError("Edit plan audio mode is invalid")
        if not isinstance(texts, list) or len(texts) > 20:
            raise ValueError("Edit plan texts are invalid")
        if not isinstance(overlays, list) or len(overlays) > 20:
            raise ValueError("Edit plan overlays are invalid")
        return cls(
            goal=goal,
            target_duration=duration,
            aspect_ratio=aspect,
            style_preset=style_name,
            pacing=pacing,
            selected_ranges=tuple(cleaned_ranges),
            removed_clip_ids=removed_ids,
            transition=transition,
            captions=dict(captions),
            audio=dict(audio),
            texts=tuple(dict(item) for item in texts if isinstance(item, dict)),
            overlays=tuple(dict(item) for item in overlays if isinstance(item, dict)),
            rationale=str(payload.get("rationale") or "")[:2000],
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "goal": self.goal,
            "target_duration": self.target_duration,
            "aspect_ratio": self.aspect_ratio,
            "style_preset": self.style_preset,
            "pacing": self.pacing,
            "selected_ranges": list(self.selected_ranges),
            "removed_clip_ids": list(self.removed_clip_ids),
            "transition": self.transition,
            "captions": self.captions,
            "audio": self.audio,
            "texts": list(self.texts),
            "overlays": list(self.overlays),
            "rationale": self.rationale,
        }


class AIEditPlanner:
    def __init__(
        self,
        registry: AIProviderRegistry | None = None,
        timelines: TimelineService | None = None,
    ) -> None:
        self.registry = registry or get_ai_provider_registry()
        self.timelines = timelines or timeline_service

    async def plan_and_apply(
        self,
        instruction: str,
        *,
        project_id: int,
        user_id: int,
        project_intelligence: dict[str, Any],
        provider_id: str,
        model: str,
    ) -> tuple[EditPlan, TimelineToolResult]:
        timeline = await self.timelines.get(project_id, user_id=user_id)
        plan = await self.generate(
            instruction,
            project_intelligence=project_intelligence,
            timeline=timeline,
            provider_id=provider_id,
            model=model,
        )
        calls = compile_tool_calls(plan, timeline, project_intelligence)
        result = await self.timelines.apply(project_id, user_id=user_id, calls=calls)
        return plan, result

    async def generate(
        self,
        instruction: str,
        *,
        project_intelligence: dict[str, Any],
        timeline: dict[str, Any],
        provider_id: str,
        model: str,
    ) -> EditPlan:
        prompt = (
            "You are a video edit planner. Return one JSON object only. Do not return FFmpeg or shell. "
            "Plan conservative edits using only attached asset IDs and current clip IDs. Required shape: "
            '{"goal":"...","target_duration":30,"aspect_ratio":"9:16",'
            '"style_preset":"reels-fast","pacing":"fast","selected_ranges":'
            '[{"asset_id":1,"start":0,"end":3}],"removed_clip_ids":[],"transition":"slide",'
            '"captions":{"enabled":true,"preset":"reels-bold"},'
            '"audio":{"mode":"keep","normalize":true,"auto_duck":false},'
            '"texts":[],"overlays":[],"rationale":"..."}. '
            f"Allowed styles: {sorted(STYLE_PRESETS)}. "
            f"Media intelligence: {json.dumps(project_intelligence, ensure_ascii=False)[:24000]}. "
            f"Timeline: {json.dumps(timeline, ensure_ascii=False)[:16000]}."
        )
        messages: list[dict[str, object]] = [
            {"role": "system", "content": prompt},
            {"role": "user", "content": instruction[:8000]},
        ]
        last_error = ""
        for attempt in range(2):
            reply = await self.registry.chat(
                provider_id,
                model,
                messages
                + ([{"role": "user", "content": last_error}] if last_error else []),
                max_reply_chars=20_000,
            )
            try:
                return EditPlan.from_payload(self._json(reply.text))
            except (ValueError, TypeError) as exc:
                if attempt:
                    raise AIProviderError(f"AI returned invalid edit plan: {exc}") from exc
                last_error = f"Plan validation failed: {exc}. Return corrected JSON only."
        raise AIProviderError("AI did not return a valid edit plan")

    @staticmethod
    def _json(text: str) -> object:
        cleaned = text.strip()
        start = cleaned.find("{")
        if start < 0:
            raise ValueError("Edit plan has no JSON object")
        try:
            value, _ = json.JSONDecoder().raw_decode(cleaned[start:])
        except json.JSONDecodeError as exc:
            raise ValueError("Edit plan JSON is malformed") from exc
        return value


def _timeline_clips(timeline: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        clip
        for track in timeline.get("tracks") or []
        if isinstance(track, dict)
        for clip in track.get("clips") or []
        if isinstance(clip, dict)
    ]


def compile_tool_calls(
    plan: EditPlan,
    timeline: dict[str, Any],
    project_intelligence: dict[str, Any],
) -> list[dict[str, Any]]:
    """Translate a validated semantic plan into deterministic Timeline tools."""

    clips = _timeline_clips(timeline)
    by_id = {str(clip.get("id")): clip for clip in clips}
    by_asset: dict[int, list[dict[str, Any]]] = {}
    for clip in clips:
        asset_id = int(clip.get("asset_id") or 0)
        if asset_id:
            by_asset.setdefault(asset_id, []).append(clip)
    analysis_by_asset = {
        int(item.get("asset_id") or 0): item
        for item in project_intelligence.get("assets") or []
        if isinstance(item, dict) and int(item.get("asset_id") or 0) > 0
    }
    calls: list[dict[str, Any]] = [
        {"name": "apply_editing_style", "arguments": {"style": plan.style_preset}},
        {"name": "set_canvas", "arguments": {"preset": plan.aspect_ratio}},
    ]
    grouped_ranges: dict[int, list[dict[str, float]]] = {}
    for item in plan.selected_ranges:
        asset_id = int(item["asset_id"])
        candidates = by_asset.get(asset_id)
        if not candidates:
            raise ValueError(f"Edit plan references unattached asset: {asset_id}")
        duration = float((analysis_by_asset.get(asset_id) or {}).get("quality", {}).get("duration") or 0)
        if duration and float(item["end"]) > duration + 0.01:
            raise ValueError(f"Edit plan range exceeds asset {asset_id} duration")
        grouped_ranges.setdefault(asset_id, []).append(
            {"start": float(item["start"]), "end": float(item["end"])}
        )
    for asset_id, ranges in grouped_ranges.items():
        calls.append(
            {
                "name": "select_ranges",
                "arguments": {"clip_id": str(by_asset[asset_id][0]["id"]), "ranges": ranges},
            }
        )
    for clip_id in plan.removed_clip_ids:
        if clip_id not in by_id:
            raise ValueError(f"Edit plan references unknown clip: {clip_id}")
        calls.append({"name": "remove_clip", "arguments": {"clip_id": clip_id}})
    if plan.transition != STYLE_PRESETS[plan.style_preset]["transition"]:
        visual = [clip for clip in clips if clip.get("asset_type") in {"video", "image"}]
        for clip in visual[:-1]:
            calls.append(
                {
                    "name": "add_transition",
                    "arguments": {"clip_id": str(clip["id"]), "type": plan.transition, "duration": 0.3},
                }
            )
    mode = str(plan.audio.get("mode") or "keep")
    if mode == "mute":
        for clip in clips:
            if clip.get("asset_type") == "video":
                calls.append(
                    {"name": "set_original_audio", "arguments": {"clip_id": str(clip["id"]), "enabled": False}}
                )
    if plan.audio.get("normalize"):
        for clip in clips:
            if clip.get("asset_type") in {"video", "audio", "voice"}:
                calls.append(
                    {"name": "normalize_audio", "arguments": {"clip_id": str(clip["id"]), "enabled": True}}
                )
    captions = plan.captions
    if captions.get("enabled"):
        cues: list[dict[str, Any]] = []
        for asset_id, analysis in analysis_by_asset.items():
            candidate = by_asset.get(asset_id, [None])[0]
            if candidate is None:
                continue
            offset = float(candidate.get("start") or 0) - float(candidate.get("source_start") or 0)
            transcription = analysis.get("transcription") or {}
            for segment in transcription.get("segments") or []:
                if not isinstance(segment, dict) or not str(segment.get("text") or "").strip():
                    continue
                cues.append(
                    {
                        "start": max(0.0, offset + float(segment.get("start") or 0)),
                        "end": max(0.04, offset + float(segment.get("end") or 0)),
                        "text": str(segment["text"])[:500],
                    }
                )
        if not cues:
            raise ValueError("Captions were requested but no cached transcription is available")
        calls.append(
            {
                "name": "add_subtitles",
                "arguments": {"cues": cues[:200], "preset": str(captions.get("preset") or "minimal")},
            }
        )
    for item in plan.texts:
        calls.append({"name": "add_text", "arguments": dict(item)})
    for item in plan.overlays:
        calls.append({"name": "add_overlay", "arguments": dict(item)})
    return calls


edit_planner = AIEditPlanner()
