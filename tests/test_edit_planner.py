from types import SimpleNamespace

import pytest

from app.services.edit_planner import (
    AIEditPlanner,
    EditPlan,
    compile_tool_calls,
    constrain_plan_for_operation,
)
from app.services.editing_styles import STYLE_PRESETS, caption_style, style
from app.services.openai_compatible import AIProviderError
from app.services.timeline import new_timeline


def _payload(**overrides):
    value = {
        "goal": "أفضل 30 ثانية",
        "target_duration": 30,
        "aspect_ratio": "9:16",
        "style_preset": "reels-fast",
        "pacing": "fast",
        "selected_ranges": [{"asset_id": 7, "start": 2, "end": 8}],
        "removed_clip_ids": [],
        "transition": "slide",
        "captions": {"enabled": True, "preset": "reels-bold"},
        "audio": {"mode": "mute", "normalize": True},
        "texts": [],
        "overlays": [],
        "rationale": "اختيار المقطع الواضح",
    }
    value.update(overrides)
    return value


def _timeline():
    value = new_timeline(1920, 1080, 30)
    value["tracks"][0]["clips"] = [
        {
            "id": "clip-video",
            "asset_id": 7,
            "asset_type": "video",
            "start": 0,
            "source_start": 0,
            "duration": 12,
            "speed": 1,
        }
    ]
    return value


def _intelligence():
    return {
        "assets": [
            {
                "asset_id": 7,
                "quality": {"duration": 12},
                "transcription": {
                    "segments": [{"start": 2, "end": 4, "text": "مرحبًا بالعالم"}]
                },
            }
        ]
    }


def test_edit_plan_validates_semantics_and_supported_presets():
    plan = EditPlan.from_payload(_payload())
    assert plan.style_preset in STYLE_PRESETS
    assert plan.selected_ranges[0] == {"asset_id": 7, "start": 2.0, "end": 8.0}
    with pytest.raises(ValueError, match="transition"):
        EditPlan.from_payload(_payload(transition="shell"))
    with pytest.raises(ValueError, match="timestamps"):
        EditPlan.from_payload(_payload(selected_ranges=[{"asset_id": 7, "start": 5, "end": 2}]))


def test_plan_compiles_to_safe_deterministic_timeline_tools():
    calls = compile_tool_calls(EditPlan.from_payload(_payload()), _timeline(), _intelligence())
    assert calls[:2] == [
        {"name": "apply_editing_style", "arguments": {"style": "reels-fast"}},
        {"name": "set_canvas", "arguments": {"preset": "9:16"}},
    ]
    assert {call["name"] for call in calls} >= {
        "select_ranges",
        "set_original_audio",
        "normalize_audio",
        "add_subtitles",
    }
    subtitle = next(call for call in calls if call["name"] == "add_subtitles")
    assert subtitle["arguments"]["cues"][0]["text"] == "مرحبًا بالعالم"
    assert all(call["name"] != "run_ffmpeg" for call in calls)


def test_plan_rejects_unknown_assets_and_missing_transcription():
    plan = EditPlan.from_payload(_payload(selected_ranges=[{"asset_id": 99, "start": 0, "end": 2}]))
    with pytest.raises(ValueError, match="unattached asset"):
        compile_tool_calls(plan, _timeline(), _intelligence())
    captions = EditPlan.from_payload(_payload(selected_ranges=[]))
    with pytest.raises(ValueError, match="no cached transcription"):
        compile_tool_calls(captions, _timeline(), {"assets": []})


def test_plan_compiles_replace_and_ducking_audio_semantics():
    timeline = _timeline()
    timeline["tracks"][1]["clips"] = [
        {
            "id": "clip-voice",
            "asset_id": 8,
            "asset_type": "voice",
            "role": "voice",
            "start": 0,
            "duration": 12,
        },
        {
            "id": "clip-music",
            "asset_id": 9,
            "asset_type": "audio",
            "role": "music",
            "start": 0,
            "duration": 12,
        },
    ]
    intelligence = {
        "assets": [
            *_intelligence()["assets"],
            {"asset_id": 8, "quality": {"duration": 12}},
            {"asset_id": 9, "quality": {"duration": 12}},
        ]
    }
    replace = EditPlan.from_payload(
        _payload(
            captions={"enabled": False},
            selected_ranges=[],
            audio={"mode": "replace", "voice_asset_id": 8},
        )
    )
    replace_calls = compile_tool_calls(replace, timeline, intelligence)
    replacement = next(
        call for call in replace_calls if call["name"] == "replace_clip_audio"
    )
    assert replacement["arguments"]["audio_asset_id"] == 8
    mix = EditPlan.from_payload(
        _payload(
            captions={"enabled": False},
            selected_ranges=[],
            audio={
                "mode": "background_music",
                "music_asset_id": 9,
                "music_volume": 0.2,
                "auto_duck": True,
            },
        )
    )
    names = [call["name"] for call in compile_tool_calls(mix, timeline, intelligence)]
    assert "set_audio_mode" in names
    assert "set_volume" in names
    assert "duck_background_music" in names


def test_high_level_remove_silence_uses_cached_analysis_not_ai_timestamps():
    plan = EditPlan.from_payload(
        _payload(captions={"enabled": False}, selected_ranges=[])
    )
    intelligence = {
        "assets": [
            {
                "asset_id": 7,
                "asset_type": "video",
                "quality": {"duration": 12},
                "audio": {
                    "speech_or_sound": [
                        {"start": 0, "end": 3.5},
                        {"start": 5, "end": 9},
                    ]
                },
            }
        ]
    }
    constrained = constrain_plan_for_operation(
        plan,
        intelligence,
        {"selected_operation": "remove_silence"},
    )
    assert constrained.selected_ranges == (
        {"asset_id": 7, "start": 0.0, "end": 3.5},
        {"asset_id": 7, "start": 5.0, "end": 9.0},
    )


def test_high_level_split_and_replace_audio_resolve_project_context():
    plan = EditPlan.from_payload(
        _payload(captions={"enabled": False}, selected_ranges=[], audio={"mode": "keep"})
    )
    intelligence = {
        "assets": [
            {"asset_id": 7, "asset_type": "video", "quality": {"duration": 75}},
            {"asset_id": 8, "asset_type": "voice", "quality": {"duration": 75}},
        ]
    }
    split = constrain_plan_for_operation(
        plan,
        intelligence,
        {"selected_operation": "split_30"},
    )
    assert split.selected_ranges[-1] == {"asset_id": 7, "start": 60.0, "end": 75.0}
    replaced = constrain_plan_for_operation(
        plan,
        intelligence,
        {
            "selected_operation": "replace_audio",
            "recent_asset": {"asset_id": 8, "asset_type": "voice"},
        },
    )
    assert replaced.audio["mode"] == "replace"
    assert replaced.audio["voice_asset_id"] == 8


def test_style_and_caption_presets_return_isolated_copies():
    first = style("cinematic")
    first["music_level"] = 99
    assert style("cinematic")["music_level"] != 99
    assert caption_style("reels-bold")["rtl"] is True


@pytest.mark.asyncio
async def test_ai_planner_retries_invalid_structured_plan_once():
    class Registry:
        def __init__(self):
            self.calls = 0

        async def chat(self, provider_id, model, messages, max_reply_chars):
            self.calls += 1
            text = "not json" if self.calls == 1 else __import__("json").dumps(_payload())
            return SimpleNamespace(text=text, model=model)

    registry = Registry()
    plan = await AIEditPlanner(registry=registry).generate(
        "أنشئ ريلز",
        project_intelligence=_intelligence(),
        timeline=_timeline(),
        provider_id="provider",
        model="model",
    )
    assert plan.style_preset == "reels-fast"
    assert registry.calls == 2


@pytest.mark.asyncio
async def test_ai_planner_fails_closed_after_two_invalid_plans():
    class Registry:
        async def chat(self, provider_id, model, messages, max_reply_chars):
            return SimpleNamespace(text='{"goal":"x","style_preset":"unknown"}', model=model)

    with pytest.raises(AIProviderError, match="invalid edit plan"):
        await AIEditPlanner(registry=Registry()).generate(
            "أنشئ ريلز",
            project_intelligence=_intelligence(),
            timeline=_timeline(),
            provider_id="provider",
            model="model",
        )
