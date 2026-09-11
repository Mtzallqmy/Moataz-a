# ruff: noqa: I001
from __future__ import annotations

import asyncio
import itertools
import json
from pathlib import Path

import pytest
from sqlalchemy import select

from app import db as database
from app.config import Settings
from app.services.composer import ComposerService
from app.services.projects import ProjectService


_USER_IDS = itertools.count(991001)


async def _fixture_assets(tmp_path: Path) -> tuple[database.User, database.MediaAsset, database.MediaAsset]:
    await database.init_db()
    image_path = tmp_path / "stored-cover.jpg"
    audio_path = tmp_path / "stored-audio.mp3"
    image_path.write_bytes(b"image-fixture")
    audio_path.write_bytes(b"audio-fixture")
    async with database.SessionLocal() as session:
        user = database.User(telegram_id=next(_USER_IDS), username="studio-projects")
        session.add(user)
        await session.flush()
        image = database.MediaAsset(
            user_id=user.id,
            asset_type="image",
            source_type="local",
            local_path=str(image_path),
            mime_type="image/jpeg",
            width=1080,
            height=1080,
            file_size=image_path.stat().st_size,
            metadata_json="{}",
        )
        audio = database.MediaAsset(
            user_id=user.id,
            asset_type="audio",
            source_type="local",
            local_path=str(audio_path),
            mime_type="audio/mpeg",
            duration=12.5,
            file_size=audio_path.stat().st_size,
            metadata_json='{"has_audio":true}',
        )
        session.add_all([image, audio])
        await session.commit()
        for item in (user, image, audio):
            session.expunge(item)
        return user, image, audio


@pytest.mark.asyncio
async def test_project_timeline_is_renderer_independent_and_composer_builds_audio_image(tmp_path: Path) -> None:
    user, image, audio = await _fixture_assets(tmp_path)
    projects = ProjectService()
    project = await projects.create_project(user_id=user.id, chat_id=123, preset="vertical")
    await projects.add_asset(project.id, image.id, user_id=user.id)
    await projects.add_asset(project.id, audio.id, user_id=user.id)
    project = await projects.apply_template(
        project.id,
        user_id=user.id,
        template="audio_image",
        options={"fit_mode": "fit"},
    )
    timeline = json.loads(project.timeline_json)
    assert timeline["version"] == 2
    assert timeline["template"] == "audio_image"
    assert "ffmpeg" not in project.timeline_json.lower()
    assert str(tmp_path) not in project.timeline_json
    plan = await ComposerService(Settings(project_dir=tmp_path), projects=projects).build(
        project.id, user_id=user.id
    )
    assert plan.template == "audio_image"
    assert plan.width == 1080
    assert plan.height == 1920
    assert plan.expected_duration == pytest.approx(12.5)
    assert [item.asset_type for item in plan.assets] == ["image", "audio"]


@pytest.mark.asyncio
async def test_project_reorder_roles_and_presets_are_persistent(tmp_path: Path) -> None:
    user, image, audio = await _fixture_assets(tmp_path)
    projects = ProjectService()
    project = await projects.create_project(user_id=user.id, chat_id=456)
    await projects.add_asset(project.id, image.id, user_id=user.id)
    await projects.add_asset(project.id, audio.id, user_id=user.id)
    assert await projects.move_asset(project.id, audio.id, user_id=user.id, direction=-1)
    await projects.set_role(project.id, audio.id, user_id=user.id, role="voice")
    await projects.set_role(project.id, image.id, user_id=user.id, role="logo")
    project = await projects.set_preset(project.id, user_id=user.id, preset="square")
    items = await projects.list_assets(project.id, user_id=user.id)
    assert [item.asset.id for item in items] == [audio.id, image.id]
    assert items[0].link.role == "voice"
    assert project.aspect_ratio == "1:1"
    assert (project.width, project.height) == (1080, 1080)
    timeline = json.loads(project.timeline_json)
    overlay_ids = {
        clip["asset_id"]
        for track in timeline["tracks"]
        if track["kind"] == "overlay"
        for clip in track["clips"]
    }
    assert image.id in overlay_ids
    async with database.SessionLocal() as session:
        revisions = list(
            await session.scalars(
                select(database.TimelineRevision).where(
                    database.TimelineRevision.project_id == project.id
                )
            )
        )
        assert len(revisions) >= 6


@pytest.mark.asyncio
async def test_concurrent_asset_additions_allocate_unique_positions(tmp_path: Path) -> None:
    user, image, audio = await _fixture_assets(tmp_path)
    projects = ProjectService()
    project = await projects.create_project(user_id=user.id, chat_id=789)

    first, second = await asyncio.gather(
        projects.add_asset(project.id, image.id, user_id=user.id),
        projects.add_asset(project.id, audio.id, user_id=user.id),
    )

    assert {first.position, second.position} == {0, 1}
    items = await projects.list_assets(project.id, user_id=user.id)
    assert [item.link.position for item in items] == [0, 1]
    assert {item.asset.id for item in items} == {image.id, audio.id}


@pytest.mark.asyncio
async def test_concurrent_duplicate_asset_addition_is_idempotent(tmp_path: Path) -> None:
    user, image, _ = await _fixture_assets(tmp_path)
    projects = ProjectService()
    project = await projects.create_project(user_id=user.id, chat_id=790)

    first, second = await asyncio.gather(
        projects.add_asset(project.id, image.id, user_id=user.id),
        projects.add_asset(project.id, image.id, user_id=user.id),
    )

    assert first.asset_id == second.asset_id == image.id
    items = await projects.list_assets(project.id, user_id=user.id)
    assert len(items) == 1
    assert items[0].link.position == 0
