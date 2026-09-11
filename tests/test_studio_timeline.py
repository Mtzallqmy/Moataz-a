from __future__ import annotations

import itertools
import json
import subprocess
from pathlib import Path

import pytest
from sqlalchemy import select

from app.config import Settings
from app.db import MediaAsset, MediaProject, ProjectAsset, SessionLocal, TimelineRevision, User, init_db
from app.services.composer import ComposerService, RenderAsset, RenderPlan
from app.services.media import probe_media_file
from app.services.projects import ProjectService
from app.services.render_service import RenderService
from app.services.renderers.ffmpeg_renderer import FFmpegRenderer
from app.services.timeline import TimelineService, new_timeline

_TELEGRAM_IDS = itertools.count(8_800_000)


def _ffmpeg(*args: str) -> None:
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args],
        check=True,
        capture_output=True,
    )


def _image(path: Path, color: str) -> Path:
    _ffmpeg("-f", "lavfi", "-i", f"color=c={color}:s=160x90", "-frames:v", "1", str(path))
    return path


def _audio(path: Path, duration: float = 2) -> Path:
    _ffmpeg(
        "-f",
        "lavfi",
        "-i",
        f"sine=frequency=440:duration={duration}",
        "-c:a",
        "libmp3lame",
        str(path),
    )
    return path


def _render_asset(asset_id: int, path: Path, asset_type: str, duration: float | None) -> RenderAsset:
    return RenderAsset(
        asset_id=asset_id,
        asset_type=asset_type,
        role="main",
        position=asset_id,
        path=path,
        duration=duration,
        width=160 if asset_type in {"image", "video"} else None,
        height=90 if asset_type in {"image", "video"} else None,
        metadata={},
    )


def _timeline(transition: str) -> dict:
    value = new_timeline(180, 320, 15)
    value["template"] = "timeline"
    value["tracks"][0]["clips"] = [
        {
            "id": "first",
            "asset_id": 1,
            "asset_type": "image",
            "start": 0,
            "source_start": 0,
            "duration": 1,
            "speed": 1,
            "volume": 1,
            "fade_in": 0,
            "fade_out": 0,
            "fit_mode": "blur-background",
            "position": 0,
            "role": "main",
            "keyframes": [],
            "transition_out": {"type": transition, "duration": 0.25},
        },
        {
            "id": "second",
            "asset_id": 2,
            "asset_type": "image",
            "start": 0.75,
            "source_start": 0,
            "duration": 1,
            "speed": 1,
            "volume": 1,
            "fade_in": 0,
            "fade_out": 0,
            "fit_mode": "fill",
            "position": 1,
            "role": "main",
            "keyframes": [],
            "transition_out": {"type": "none", "duration": 0},
        },
    ]
    value["tracks"][1]["clips"] = [
        {
            "id": "music",
            "asset_id": 3,
            "asset_type": "audio",
            "start": 0,
            "source_start": 0,
            "duration": 1.75,
            "speed": 1,
            "volume": 0.2,
            "fade_in": 0.1,
            "fade_out": 0.1,
            "fit_mode": "fit",
            "position": 0,
            "role": "music",
            "keyframes": [],
            "transition_out": {"type": "none", "duration": 0},
        }
    ]
    return value


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "transition",
    ["fade", "dissolve", "slide", "wipe", "zoom", "blur", "push", "dip-to-black"],
)
async def test_timeline_renderer_supports_real_transitions(tmp_path: Path, transition: str) -> None:
    first = _image(tmp_path / "first.png", "red")
    second = _image(tmp_path / "second.png", "blue")
    music = _audio(tmp_path / "music.mp3")
    timeline = _timeline(transition)
    plan = RenderPlan(
        project_id=900,
        user_id=901,
        template="timeline",
        width=180,
        height=320,
        fps=15,
        aspect_ratio="9:16",
        fit_mode="fit",
        transition=transition,
        audio_mode="mix_audio",
        logo_position="top-right",
        assets=(
            _render_asset(1, first, "image", None),
            _render_asset(2, second, "image", None),
            _render_asset(3, music, "audio", 2),
        ),
        expected_duration=1.75,
        timeline=timeline,
    )
    settings = Settings(project_dir=tmp_path / "projects", render_temp_dir=tmp_path / "renders")
    result = await FFmpegRenderer(settings).render(plan, render_job_id=100)
    probe = await probe_media_file(result.output_path)
    assert probe.has_video and probe.has_audio
    assert (probe.width, probe.height) == (180, 320)
    assert probe.duration == pytest.approx(1.75, abs=0.3)


@pytest.mark.asyncio
async def test_timeline_renderer_composites_overlay_text_captions_and_audio_tracks(
    tmp_path: Path,
) -> None:
    first = _image(tmp_path / "first.png", "red")
    second = _image(tmp_path / "second.png", "blue")
    logo = _image(tmp_path / "logo.png", "yellow")
    music = _audio(tmp_path / "music.mp3")
    timeline = _timeline("dissolve")
    timeline["tracks"][0]["clips"][0].update(
        {"crop": {"left": 0.1, "top": 0, "right": 0.1, "bottom": 0}, "scale": 0.9}
    )
    timeline["tracks"][1]["clips"].append(
        {
            **timeline["tracks"][1]["clips"][0],
            "id": "voice",
            "start": 0.25,
            "volume": 0.5,
        }
    )
    timeline["tracks"][2]["clips"] = [
        {
            "id": "logo",
            "asset_id": 4,
            "asset_type": "logo",
            "start": 0,
            "duration": 1.75,
            "speed": 1,
            "volume": 0,
            "fit_mode": "fit",
            "position_name": "bottom-left",
            "scale": 0.2,
            "keyframes": [],
            "transition_out": {"type": "none", "duration": 0},
        }
    ]
    timeline["tracks"][3]["clips"] = [
        {
            "id": "title",
            "text": "Moataz",
            "start": 0,
            "duration": 0.8,
            "speed": 1,
            "volume": 0,
            "fit_mode": "fit",
            "position_name": "center",
            "font_size": 28,
            "color": "white",
            "keyframes": [],
            "transition_out": {"type": "none", "duration": 0},
        }
    ]
    timeline["tracks"][4]["clips"] = [
        {
            **timeline["tracks"][3]["clips"][0],
            "id": "caption",
            "text": "subtitle",
            "start": 0.8,
            "position_name": "bottom",
        }
    ]
    plan = RenderPlan(
        project_id=902,
        user_id=903,
        template="timeline",
        width=180,
        height=320,
        fps=15,
        aspect_ratio="9:16",
        fit_mode="fit",
        transition="dissolve",
        audio_mode="mix_audio",
        logo_position="bottom-left",
        assets=(
            _render_asset(1, first, "image", None),
            _render_asset(2, second, "image", None),
            _render_asset(3, music, "audio", 2),
            _render_asset(4, logo, "logo", None),
        ),
        expected_duration=1.75,
        timeline=timeline,
    )
    settings = Settings(project_dir=tmp_path / "projects", render_temp_dir=tmp_path / "renders")
    result = await FFmpegRenderer(settings).render(plan, render_job_id=101)
    probe = await probe_media_file(result.output_path)
    assert probe.has_video and probe.has_audio
    assert probe.duration == pytest.approx(1.75, abs=0.3)


async def _database_project(tmp_path: Path) -> tuple[int, int, ProjectService, TimelineService]:
    await init_db()
    settings = Settings(project_dir=tmp_path / "projects", render_temp_dir=tmp_path / "renders")
    projects = ProjectService(settings)
    timelines = TimelineService(settings)
    async with SessionLocal() as session:
        user = User(telegram_id=next(_TELEGRAM_IDS))
        session.add(user)
        await session.flush()
        project = MediaProject(
            user_id=user.id,
            chat_id=user.telegram_id,
            name="Timeline",
            width=180,
            height=320,
            fps=15,
            timeline_json=json.dumps(new_timeline(180, 320, 15)),
        )
        session.add(project)
        await session.flush()
        image_path = settings.project_dir / str(project.id) / "assets" / "1" / "source.png"
        image_path.parent.mkdir(parents=True)
        _image(image_path, "green")
        asset = MediaAsset(
            user_id=user.id,
            asset_type="image",
            source_type="local",
            local_path=str(image_path),
            mime_type="image/png",
            width=160,
            height=90,
            file_size=image_path.stat().st_size,
        )
        session.add(asset)
        await session.flush()
        session.add(ProjectAsset(project_id=project.id, asset_id=asset.id, position=0, role="main"))
        audio_path = settings.project_dir / str(project.id) / "assets" / "2" / "source.mp3"
        audio_path.parent.mkdir(parents=True)
        _audio(audio_path, duration=4)
        audio = MediaAsset(
            user_id=user.id,
            asset_type="audio",
            source_type="local",
            local_path=str(audio_path),
            mime_type="audio/mpeg",
            duration=10,
            file_size=audio_path.stat().st_size,
        )
        session.add(audio)
        await session.flush()
        session.add(ProjectAsset(project_id=project.id, asset_id=audio.id, position=1, role="music"))
        await session.commit()
        return user.id, project.id, projects, timelines


async def _audio_edit_project(tmp_path: Path) -> tuple[int, int, TimelineService, int, int, int]:
    await init_db()
    settings = Settings(project_dir=tmp_path / "projects", render_temp_dir=tmp_path / "renders")
    service = TimelineService(settings)
    async with SessionLocal() as session:
        user = User(telegram_id=next(_TELEGRAM_IDS))
        session.add(user)
        await session.flush()
        project = MediaProject(
            user_id=user.id,
            chat_id=user.telegram_id,
            name="Audio editing",
            width=320,
            height=180,
            fps=24,
            timeline_json=json.dumps(new_timeline(320, 180, 24)),
        )
        session.add(project)
        await session.flush()
        definitions = [
            ("video", "video.mp4", 8.0, "main"),
            ("audio", "music.mp3", 8.0, "music"),
            ("voice", "voice.ogg", 3.0, "voice"),
        ]
        assets: list[MediaAsset] = []
        for position, (asset_type, filename, duration, role) in enumerate(definitions):
            path = tmp_path / filename
            path.write_bytes(b"fixture")
            asset = MediaAsset(
                user_id=user.id,
                asset_type=asset_type,
                source_type="local",
                local_path=str(path),
                mime_type="video/mp4" if asset_type == "video" else "audio/mpeg",
                duration=duration,
                width=320 if asset_type == "video" else None,
                height=180 if asset_type == "video" else None,
                file_size=path.stat().st_size,
            )
            session.add(asset)
            await session.flush()
            assets.append(asset)
            session.add(
                ProjectAsset(
                    project_id=project.id,
                    asset_id=asset.id,
                    position=position,
                    role=role,
                )
            )
        await session.commit()
        return user.id, project.id, service, assets[0].id, assets[1].id, assets[2].id


@pytest.mark.asyncio
async def test_timeline_tools_modify_same_project_and_support_undo_redo(tmp_path: Path) -> None:
    user_id, project_id, _, service = await _database_project(tmp_path)
    initial = await service.get(project_id, user_id=user_id)
    clip_id = initial["tracks"][0]["clips"][0]["id"]
    result = await service.apply(
        project_id,
        user_id=user_id,
        calls=[
            {"name": "trim_clip", "arguments": {"clip_id": clip_id, "source_start": 0, "source_end": 3}},
            {"name": "set_speed", "arguments": {"clip_id": clip_id, "speed": 2}},
            {"name": "add_transition", "arguments": {"clip_id": clip_id, "type": "wipe", "duration": 0.2}},
            {"name": "add_text", "arguments": {"text": "عنوان", "start": 0, "duration": 1}},
            {"name": "set_canvas", "arguments": {"preset": "1:1"}},
        ],
    )
    assert result.timeline["revision"] == 1
    assert result.timeline["canvas"]["width"] == 1080
    assert result.timeline["tracks"][0]["clips"][0]["speed"] == 2
    assert result.timeline["tracks"][3]["clips"][0]["text"] == "عنوان"

    undone = await service.undo(project_id, user_id=user_id)
    assert undone.timeline["tracks"][0]["clips"][0]["speed"] == 1
    redone = await service.redo(project_id, user_id=user_id)
    assert redone.timeline["tracks"][0]["clips"][0]["speed"] == 2
    async with SessionLocal() as session:
        revisions = list(
            await session.scalars(
                select(TimelineRevision).where(TimelineRevision.project_id == project_id)
            )
        )
        assert len(revisions) == 4


@pytest.mark.asyncio
async def test_timeline_rejects_invalid_calls_and_cross_user_access(tmp_path: Path) -> None:
    user_id, project_id, _, service = await _database_project(tmp_path)
    with pytest.raises(LookupError):
        await service.get(project_id, user_id=user_id + 999)
    with pytest.raises(ValueError, match="Unknown or invalid agent tool"):
        await service.apply(
            project_id,
            user_id=user_id,
            calls=[{"name": "run_shell", "arguments": {"command": "rm -rf /"}}],
        )


@pytest.mark.asyncio
async def test_timeline_tool_catalog_handles_multitrack_edits(tmp_path: Path) -> None:
    user_id, project_id, _, service = await _database_project(tmp_path)
    timeline = await service.get(project_id, user_id=user_id)
    visual = timeline["tracks"][0]["clips"][0]
    audio = timeline["tracks"][1]["clips"][0]
    split = await service.apply(
        project_id,
        user_id=user_id,
        calls=[
            {"name": "split_clip", "arguments": {"clip_id": visual["id"], "at": 1}},
            {"name": "set_volume", "arguments": {"clip_id": audio["id"], "volume": 0.15}},
            {"name": "set_fades", "arguments": {"clip_id": audio["id"], "fade_in": 0.2, "fade_out": 0.3}},
            {"name": "set_transform", "arguments": {"clip_id": visual["id"], "scale": 0.8, "x": 0.2, "crop": {"left": 0.1}}},
            {"name": "set_keyframes", "arguments": {"clip_id": visual["id"], "keyframes": [{"time": 0.5, "property": "scale", "value": 1.1}]}},
            {"name": "add_subtitles", "arguments": {"cues": [{"start": 0, "end": 1, "text": "مرحباً"}]}},
            {"name": "add_overlay", "arguments": {"asset_id": visual["asset_id"], "start": 0, "duration": 1, "position": "top-left"}},
            {"name": "add_background_music", "arguments": {"asset_id": audio["asset_id"], "duration": 3, "volume": 0.1}},
            {"name": "set_fit_mode", "arguments": {"mode": "blur-background", "apply_to_all": True}},
            {"name": "set_audio_mode", "arguments": {"mode": "background_music"}},
        ],
    )
    visuals = split.timeline["tracks"][0]["clips"]
    assert len(visuals) == 2
    assert visuals[0]["scale"] == 0.8
    assert visuals[0]["keyframes"][0]["property"] == "scale"
    assert split.timeline["tracks"][1]["clips"][0]["volume"] == 0.15
    assert split.timeline["tracks"][4]["clips"][0]["text"] == "مرحباً"
    assert len(split.timeline["tracks"][2]["clips"]) == 1
    assert len(split.timeline["tracks"][1]["clips"]) == 2
    assert split.timeline["options"]["audio_mode"] == "background_music"

    order = [visuals[1]["id"], visuals[0]["id"]]
    reordered = await service.apply(
        project_id,
        user_id=user_id,
        calls=[
            {"name": "reorder_clips", "arguments": {"clip_id": visuals[0]["id"], "order": order}},
            {"name": "move_clip", "arguments": {"clip_id": visuals[0]["id"], "start": 9}},
        ],
    )
    assert [clip["id"] for clip in reordered.timeline["tracks"][0]["clips"]] == order
    with pytest.raises(ValueError, match="not attached"):
        await service.apply(
            project_id,
            user_id=user_id,
            calls=[{"name": "add_clip", "arguments": {"asset_id": 99999}}],
        )


@pytest.mark.asyncio
async def test_timeline_audio_tools_mute_replace_and_duck_with_undo(tmp_path: Path) -> None:
    user_id, project_id, service, video_id, music_id, voice_id = await _audio_edit_project(
        tmp_path
    )
    timeline = await service.get(project_id, user_id=user_id)
    video = next(
        clip
        for track in timeline["tracks"]
        for clip in track["clips"]
        if clip.get("asset_id") == video_id
    )
    music = next(
        clip
        for track in timeline["tracks"]
        for clip in track["clips"]
        if clip.get("asset_id") == music_id
    )
    edited = await service.apply(
        project_id,
        user_id=user_id,
        calls=[
            {
                "name": "set_original_audio",
                "arguments": {"clip_id": video["id"], "enabled": False},
            },
            {
                "name": "set_volume_range",
                "arguments": {
                    "clip_id": music["id"],
                    "start": 1,
                    "end": 2,
                    "volume": 0,
                },
            },
            {
                "name": "replace_clip_audio",
                "arguments": {
                    "clip_id": video["id"],
                    "audio_asset_id": voice_id,
                    "volume": 0.8,
                },
            },
            {
                "name": "duck_background_music",
                "arguments": {"clip_id": music["id"], "volume": 0.15},
            },
        ],
    )
    edited_video = next(
        clip
        for track in edited.timeline["tracks"]
        for clip in track["clips"]
        if clip.get("id") == video["id"]
    )
    edited_music = next(
        clip
        for track in edited.timeline["tracks"]
        for clip in track["clips"]
        if clip.get("id") == music["id"]
    )
    replacement = next(
        clip
        for track in edited.timeline["tracks"]
        for clip in track["clips"]
        if clip.get("asset_id") == voice_id
    )
    assert edited_video["original_audio_enabled"] is False
    assert replacement["volume"] == pytest.approx(0.8)
    assert replacement["start"] == edited_video["start"]
    assert any(item["volume"] == 0 for item in edited_music["volume_ranges"])
    assert any(item["volume"] == pytest.approx(0.15) for item in edited_music["volume_ranges"])
    assert edited.timeline["options"]["audio_mode"] == "mix_audio"

    undone = await service.undo(project_id, user_id=user_id)
    original_video = next(
        clip
        for track in undone.timeline["tracks"]
        for clip in track["clips"]
        if clip.get("asset_id") == video_id
    )
    assert "original_audio_enabled" not in original_video


@pytest.mark.asyncio
async def test_timeline_rejects_invalid_partial_mute_range(tmp_path: Path) -> None:
    user_id, project_id, service, video_id, _, _ = await _audio_edit_project(tmp_path)
    timeline = await service.get(project_id, user_id=user_id)
    video = next(
        clip
        for track in timeline["tracks"]
        for clip in track["clips"]
        if clip.get("asset_id") == video_id
    )
    with pytest.raises(ValueError, match="exceeds clip duration"):
        await service.apply(
            project_id,
            user_id=user_id,
            calls=[
                {
                    "name": "set_volume_range",
                    "arguments": {
                        "clip_id": video["id"],
                        "start": 2,
                        "end": 20,
                        "volume": 0,
                    },
                }
            ],
        )


@pytest.mark.asyncio
async def test_composer_builds_low_resolution_bounded_preview(tmp_path: Path) -> None:
    user_id, project_id, projects, timelines = await _database_project(tmp_path)
    timeline = await timelines.get(project_id, user_id=user_id)
    clip_id = timeline["tracks"][0]["clips"][0]["id"]
    await timelines.apply(
        project_id,
        user_id=user_id,
        calls=[{"name": "set_duration", "arguments": {"clip_id": clip_id, "duration": 4}}],
    )
    plan = await ComposerService(settings=projects.settings, projects=projects).build(
        project_id,
        user_id=user_id,
        render_kind="preview",
        preview_duration=2,
        preview_width=320,
    )
    assert plan.template == "timeline"
    assert plan.render_kind == "preview"
    assert max(plan.width, plan.height) <= 320
    assert plan.expected_duration == 2


@pytest.mark.asyncio
async def test_timeline_preview_renders_end_to_end_and_keeps_project_editable(
    tmp_path: Path,
) -> None:
    user_id, project_id, projects, timelines = await _database_project(tmp_path)
    timeline = await timelines.get(project_id, user_id=user_id)
    clip_id = timeline["tracks"][0]["clips"][0]["id"]
    edit = await timelines.apply(
        project_id,
        user_id=user_id,
        calls=[
            {"name": "set_duration", "arguments": {"clip_id": clip_id, "duration": 3}},
            {"name": "add_text", "arguments": {"text": "Preview", "start": 0, "duration": 1}},
            {"name": "render_preview", "arguments": {}},
        ],
    )
    assert edit.render_action == "preview"
    settings = projects.settings
    service = RenderService(
        settings,
        composer=ComposerService(settings=settings, projects=projects),
        renderer=FFmpegRenderer(settings),
    )
    job = await service.create_render(
        project_id,
        user_id=user_id,
        kind="preview",
        preview_duration=2,
        preview_width=320,
    )
    await service.process_render(job.id)
    stored = await service.get_render(job.id, user_id=user_id)
    assert stored is not None and stored.status == "COMPLETED"
    assert stored.output_path
    probe = await probe_media_file(Path(stored.output_path))
    assert probe.has_video and probe.has_audio
    assert max(probe.width or 0, probe.height or 0) <= 320
    async with SessionLocal() as session:
        project = await session.get(MediaProject, project_id)
        assert project is not None and project.status == "READY"
