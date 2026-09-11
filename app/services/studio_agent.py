from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select

from app.db import MediaProject, SessionLocal, StudioAgentMessage
from app.services.ai_registry import AIProviderRegistry, get_ai_provider_registry
from app.services.edit_planner import AIEditPlanner, compile_tool_calls
from app.services.media_intelligence import MediaIntelligenceService, media_intelligence_service
from app.services.openai_compatible import AIProviderError
from app.services.timeline import TOOL_NAMES, TimelineService, timeline_service


@dataclass(frozen=True, slots=True)
class StudioAgentReply:
    text: str
    model: str
    applied_tools: tuple[str, ...]
    render_action: str | None
    timeline: dict[str, Any]


_REQUIRED_ARGUMENTS: dict[str, tuple[str, ...]] = {
    "inspect_asset": ("asset_id",),
    "add_clip": ("asset_id",),
    "remove_clip": ("clip_id",),
    "trim_clip": ("clip_id", "source_end"),
    "split_clip": ("clip_id", "at"),
    "move_clip": ("clip_id", "start"),
    "reorder_clips": ("clip_id", "order"),
    "set_duration": ("clip_id", "duration"),
    "set_speed": ("clip_id", "speed"),
    "set_volume": ("clip_id", "volume"),
    "normalize_audio": ("clip_id",),
    "set_volume_range": ("clip_id", "start", "end", "volume"),
    "set_original_audio": ("clip_id", "enabled"),
    "replace_clip_audio": ("clip_id", "audio_asset_id"),
    "duck_background_music": ("clip_id",),
    "set_fades": ("clip_id",),
    "set_transform": ("clip_id",),
    "add_transition": ("clip_id", "type"),
    "add_text": ("text",),
    "add_overlay": ("asset_id",),
    "add_background_music": ("asset_id",),
    "set_audio_mode": ("mode",),
    "set_keyframes": ("clip_id", "keyframes"),
    "select_ranges": ("clip_id", "ranges"),
    "apply_editing_style": ("style",),
    "add_subtitles": ("cues",),
}

_PLANNING_MARKERS = {
    "أفضل",
    "احترافي",
    "أسلوب",
    "سينمائي",
    "اختر اللقطات",
    "احذف الصمت",
    "إزالة الصمت",
    "كابتشن",
    "ترجمة",
    "cinematic",
    "best moments",
    "highlight reel",
    "remove silence",
    "captions",
    "documentary",
}


def requires_edit_plan(instruction: str) -> bool:
    """Route semantic, multi-step requests through analysis before Timeline tools."""

    normalized = " ".join(instruction.casefold().split())
    if any(marker in normalized for marker in _PLANNING_MARKERS):
        return True
    separators = normalized.count(" ثم ") + normalized.count("،") + normalized.count(",")
    return separators >= 2


def _tool(name: str, description: str, properties: dict[str, Any] | None = None) -> dict[str, Any]:
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": properties or {},
        "additionalProperties": False,
    }
    required = _REQUIRED_ARGUMENTS.get(name)
    if required:
        parameters["required"] = list(required)
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": parameters,
        },
    }


AGENT_TOOLS: list[dict[str, Any]] = [
    _tool("list_assets", "List media assets attached to the current project."),
    _tool("inspect_asset", "Inspect one project asset.", {"asset_id": {"type": "integer"}}),
    _tool("add_clip", "Add an attached asset to a timeline track.", {"asset_id": {"type": "integer"}, "track": {"type": "string"}, "start": {"type": "number"}, "duration": {"type": "number"}}),
    _tool("remove_clip", "Remove a clip non-destructively.", {"clip_id": {"type": "string"}}),
    _tool("trim_clip", "Trim source boundaries in seconds.", {"clip_id": {"type": "string"}, "source_start": {"type": "number"}, "source_end": {"type": "number"}}),
    _tool("split_clip", "Split a clip at a relative second.", {"clip_id": {"type": "string"}, "at": {"type": "number"}}),
    _tool("move_clip", "Move a clip to a timeline start time.", {"clip_id": {"type": "string"}, "start": {"type": "number"}}),
    _tool("reorder_clips", "Reorder every clip in a track.", {"clip_id": {"type": "string"}, "order": {"type": "array", "items": {"type": "string"}}}),
    _tool("set_duration", "Set a clip duration.", {"clip_id": {"type": "string"}, "duration": {"type": "number"}}),
    _tool("set_speed", "Set playback speed from 0.25 to 4.", {"clip_id": {"type": "string"}, "speed": {"type": "number"}}),
    _tool("set_volume", "Set audio volume from 0 to 2.", {"clip_id": {"type": "string"}, "volume": {"type": "number"}}),
    _tool("normalize_audio", "Normalize loudness safely for a video or audio clip.", {"clip_id": {"type": "string"}, "enabled": {"type": "boolean"}}),
    _tool("set_volume_range", "Set or mute a clip's audio only within a relative time range.", {"clip_id": {"type": "string"}, "start": {"type": "number"}, "end": {"type": "number"}, "volume": {"type": "number"}}),
    _tool("set_original_audio", "Enable or completely remove a video clip's original audio.", {"clip_id": {"type": "string"}, "enabled": {"type": "boolean"}, "volume": {"type": "number"}}),
    _tool("replace_clip_audio", "Replace one video clip's original audio with an attached audio or voice asset; optionally mix the original.", {"clip_id": {"type": "string"}, "audio_asset_id": {"type": "integer"}, "volume": {"type": "number"}, "mix_original": {"type": "boolean"}}),
    _tool("duck_background_music", "Lower a music clip during overlapping voice clips.", {"clip_id": {"type": "string"}, "volume": {"type": "number"}, "release": {"type": "number"}}),
    _tool("set_fades", "Set media fade-in and fade-out seconds.", {"clip_id": {"type": "string"}, "fade_in": {"type": "number"}, "fade_out": {"type": "number"}}),
    _tool("set_transform", "Set crop, scale, and normalized position.", {"clip_id": {"type": "string"}, "scale": {"type": "number"}, "x": {"type": "number"}, "y": {"type": "number"}, "crop": {"type": "object"}}),
    _tool("add_transition", "Add fade, dissolve, slide, wipe, zoom, blur, push, or dip-to-black.", {"clip_id": {"type": "string"}, "type": {"type": "string"}, "duration": {"type": "number"}}),
    _tool("add_text", "Add timed text or a title.", {"text": {"type": "string"}, "start": {"type": "number"}, "duration": {"type": "number"}, "position": {"type": "string"}, "font_size": {"type": "integer"}, "color": {"type": "string"}}),
    _tool("add_overlay", "Use an attached image/logo/video as an overlay.", {"asset_id": {"type": "integer"}, "start": {"type": "number"}, "duration": {"type": "number"}, "position": {"type": "string"}, "scale": {"type": "number"}}),
    _tool("add_background_music", "Add attached audio as quiet background music.", {"asset_id": {"type": "integer"}, "start": {"type": "number"}, "duration": {"type": "number"}, "volume": {"type": "number"}}),
    _tool("set_audio_mode", "Set replace_audio, mix_audio, or background_music.", {"mode": {"type": "string"}}),
    _tool("set_canvas", "Set 9:16, 16:9, 1:1, or safe custom dimensions.", {"preset": {"type": "string"}, "width": {"type": "integer"}, "height": {"type": "integer"}}),
    _tool("set_fit_mode", "Set fit, fill, or blur-background.", {"clip_id": {"type": "string"}, "mode": {"type": "string"}, "apply_to_all": {"type": "boolean"}}),
    _tool("set_keyframes", "Store validated extensible keyframes.", {"clip_id": {"type": "string"}, "keyframes": {"type": "array", "items": {"type": "object"}}}),
    _tool("select_ranges", "Keep selected timestamp ranges from one clip.", {"clip_id": {"type": "string"}, "ranges": {"type": "array", "items": {"type": "object"}}}),
    _tool("apply_editing_style", "Apply a validated editing style preset.", {"style": {"type": "string"}}),
    _tool("add_subtitles", "Add validated timed caption cues.", {"cues": {"type": "array", "items": {"type": "object"}}, "preset": {"type": "string"}, "style": {"type": "object"}}),
    _tool("undo", "Undo the most recent atomic timeline revision."),
    _tool("redo", "Redo the most recently undone timeline revision."),
    _tool("render_preview", "Request a short low-resolution preview."),
    _tool("render_final", "Request the final render."),
]


def _parse_json_reply(text: str) -> tuple[str, list[dict[str, Any]]]:
    cleaned = text.strip()
    if "```" in cleaned:
        blocks = cleaned.split("```")
        cleaned = next((block.removeprefix("json").strip() for block in blocks if "{" in block), cleaned)
    start = cleaned.find("{")
    if start < 0:
        raise ValueError("AI response does not contain JSON")
    try:
        payload, _ = json.JSONDecoder().raw_decode(cleaned[start:])
    except json.JSONDecodeError as exc:
        raise ValueError("AI response contains invalid JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError("AI response must be a JSON object")
    calls = payload.get("tool_calls")
    if not isinstance(calls, list) or not calls:
        raise ValueError("AI response must include tool_calls")
    normalized: list[dict[str, Any]] = []
    for call in calls:
        if not isinstance(call, dict):
            raise ValueError("AI tool call must be an object")
        name = str(call.get("name") or "")
        arguments = call.get("arguments") or {}
        if name not in TOOL_NAMES or not isinstance(arguments, dict):
            raise ValueError(f"AI requested an invalid tool: {name}")
        normalized.append({"name": name, "arguments": arguments})
    return str(payload.get("message") or "تم تحديث المشروع."), normalized


class StudioAgentService:
    def __init__(
        self,
        *,
        registry: AIProviderRegistry | None = None,
        timelines: TimelineService | None = None,
        planner: AIEditPlanner | None = None,
        intelligence: MediaIntelligenceService | None = None,
    ) -> None:
        self.registry = registry or get_ai_provider_registry()
        self.timelines = timelines or timeline_service
        self.planner = planner or AIEditPlanner(
            registry=self.registry, timelines=self.timelines
        )
        self.intelligence = intelligence or media_intelligence_service

    async def handle(
        self,
        project_id: int,
        *,
        user_id: int,
        instruction: str,
        provider_id: str,
        model: str,
        native_tools: bool = False,
    ) -> StudioAgentReply:
        instruction = instruction.strip()
        if not instruction or len(instruction) > 8000:
            raise ValueError("Instruction must contain 1-8000 characters")
        timeline = await self.timelines.get(project_id, user_id=user_id)
        history = await self._history(project_id, user_id=user_id)
        await self._store(project_id, "user", instruction, provider_id, model)
        system = self._system_prompt(timeline)
        messages: list[dict[str, object]] = [
            {"role": "system", "content": system},
            *history,
            {"role": "user", "content": instruction},
        ]
        if requires_edit_plan(instruction):
            return await self._planned(
                project_id,
                user_id=user_id,
                instruction=instruction,
                provider_id=provider_id,
                model=model,
                timeline=timeline,
                history=history,
            )
        calls: list[dict[str, Any]]
        response_text = ""
        response_model = model
        if native_tools:
            try:
                native = await self.registry.chat_tools(provider_id, model, messages, AGENT_TOOLS)
                calls = list(native.tool_calls)
                response_text = native.text
                response_model = native.model
                if not calls:
                    response_text, calls = _parse_json_reply(native.text)
            except (AIProviderError, ValueError):
                response_text, calls, response_model = await self._structured(
                    provider_id, model, messages
                )
        else:
            response_text, calls, response_model = await self._structured(provider_id, model, messages)
        try:
            result = await self.timelines.apply(project_id, user_id=user_id, calls=calls)
        except (ValueError, LookupError) as exc:
            retry_messages = [
                *messages,
                {"role": "assistant", "content": json.dumps({"tool_calls": calls})},
                {"role": "user", "content": f"Validation failed: {exc}. Return one corrected JSON object."},
            ]
            response_text, calls, response_model = await self._structured(
                provider_id, model, retry_messages
            )
            result = await self.timelines.apply(project_id, user_id=user_id, calls=calls)
        final_text = response_text or "تم تطبيق التعديلات على نفس Timeline."
        await self._store(project_id, "assistant", final_text, provider_id, response_model)
        return StudioAgentReply(
            text=final_text,
            model=response_model,
            applied_tools=tuple(str(call["name"]) for call in calls),
            render_action=result.render_action,
            timeline=result.timeline,
        )

    async def _planned(
        self,
        project_id: int,
        *,
        user_id: int,
        instruction: str,
        provider_id: str,
        model: str,
        timeline: dict[str, Any],
        history: list[dict[str, object]],
    ) -> StudioAgentReply:
        intelligence = await self.intelligence.analyze_project(
            project_id, user_id=user_id
        )
        validation_feedback = ""
        calls: list[dict[str, Any]] = []
        plan = None
        for attempt in range(2):
            plan = await self.planner.generate(
                instruction,
                project_intelligence=intelligence,
                timeline=timeline,
                provider_id=provider_id,
                model=model,
                conversation_history=history,
                validation_feedback=validation_feedback,
            )
            try:
                calls = compile_tool_calls(plan, timeline, intelligence)
                break
            except ValueError as exc:
                if attempt:
                    raise AIProviderError(
                        f"AI edit plan failed deterministic validation: {exc}"
                    ) from exc
                validation_feedback = str(exc)
        if plan is None:
            raise AIProviderError("AI did not produce an edit plan")
        render_action = self._requested_render(instruction)
        if render_action:
            calls.append({"name": f"render_{render_action}", "arguments": {}})
        result = await self.timelines.apply(project_id, user_id=user_id, calls=calls)
        text = f"تم تطبيق خطة المونتاج: {plan.goal}"
        if plan.rationale:
            text += f" — {plan.rationale[:500]}"
        await self._store(project_id, "assistant", text, provider_id, model)
        return StudioAgentReply(
            text=text,
            model=model,
            applied_tools=tuple(str(call["name"]) for call in calls),
            render_action=result.render_action,
            timeline=result.timeline,
        )

    @staticmethod
    def _requested_render(instruction: str) -> str | None:
        normalized = instruction.casefold()
        if "معاينة" in normalized or "preview" in normalized:
            return "preview"
        if any(value in normalized for value in ("تصدير نهائي", "الرندر النهائي", "final render")):
            return "final"
        return None

    async def _structured(
        self,
        provider_id: str,
        model: str,
        messages: list[dict[str, object]],
    ) -> tuple[str, list[dict[str, Any]], str]:
        contracts = [
            {
                "name": item["function"]["name"],
                "description": item["function"]["description"],
                "required": item["function"]["parameters"].get("required", []),
                "properties": item["function"]["parameters"].get("properties", {}),
            }
            for item in AGENT_TOOLS
        ]
        schema_instruction = {
            "role": "system",
            "content": (
                "Native tools are unavailable. Return JSON only: "
                '{"message":"short Arabic summary","tool_calls":[{"name":"tool","arguments":{}}]}. '
                "Arguments listed as required must never be omitted or empty. For clip-targeting tools, "
                "copy an exact clip_id from the current timeline. Never return shell or FFmpeg commands. "
                "Tool contracts: "
                + json.dumps(contracts, ensure_ascii=False, separators=(",", ":"))
            ),
        }
        last_error = ""
        for attempt in range(2):
            reply = await self.registry.chat(
                provider_id,
                model,
                [*messages, schema_instruction]
                + ([{"role": "user", "content": last_error}] if last_error else []),
                max_reply_chars=20_000,
            )
            try:
                text, calls = _parse_json_reply(reply.text)
                return text, calls, reply.model
            except ValueError as exc:
                if attempt:
                    raise AIProviderError(f"AI returned invalid editing JSON: {exc}") from exc
                last_error = f"Your JSON was rejected: {exc}. Correct it and return JSON only."
        raise AIProviderError("AI did not return valid editing JSON")

    async def _history(self, project_id: int, *, user_id: int) -> list[dict[str, object]]:
        async with SessionLocal() as session:
            owned = await session.scalar(
                select(MediaProject.id).where(
                    MediaProject.id == project_id, MediaProject.user_id == user_id
                )
            )
            if owned is None:
                raise LookupError("Project not found")
            rows = list(
                await session.scalars(
                    select(StudioAgentMessage)
                    .where(StudioAgentMessage.project_id == project_id)
                    .order_by(StudioAgentMessage.id.desc())
                    .limit(12)
                )
            )
            return [
                {"role": row.role, "content": row.content}
                for row in reversed(rows)
                if row.role in {"user", "assistant"}
            ]

    @staticmethod
    async def _store(
        project_id: int,
        role: str,
        content: str,
        provider_id: str,
        model: str,
    ) -> None:
        async with SessionLocal() as session:
            session.add(
                StudioAgentMessage(
                    project_id=project_id,
                    role=role,
                    content=content[:12_000],
                    provider_id=provider_id[:64] or None,
                    model=model[:255] or None,
                )
            )
            await session.commit()

    @staticmethod
    def _system_prompt(timeline: dict[str, Any]) -> str:
        compact = json.dumps(timeline, ensure_ascii=False, separators=(",", ":"))[:24_000]
        return (
            "You are Moataz Media Studio's editing planner. Modify the existing project, never create "
            "a new project. You cannot run shell commands or FFmpeg. Use only the supplied deterministic "
            "tools. Asset IDs and clip IDs must come from the timeline. For every clip-targeting tool, "
            "copy a non-empty exact clip id from the timeline; never invent or omit it. Make conservative, reversible edits. "
            "For full video mute use set_original_audio(enabled=false); for a timed mute use "
            "set_volume_range(volume=0). Use replace_clip_audio for a specific video and attached "
            "audio/voice asset. Use duck_background_music to lower music during overlapping voice. "
            "When the user asks to see the result, request render_preview; request render_final only when explicit. "
            f"Current renderer-neutral Timeline JSON: {compact}"
        )


studio_agent_service = StudioAgentService()
