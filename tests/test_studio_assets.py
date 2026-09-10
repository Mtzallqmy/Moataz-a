from __future__ import annotations

import itertools
import subprocess
from pathlib import Path

import pytest

from app.config import Settings
from app.db import MediaAsset, MediaProject, SessionLocal, User, init_db
from app.services.assets import AssetService


def _ffmpeg(*args: str) -> None:
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args],
        check=True,
        capture_output=True,
    )


_USER_IDS = itertools.count(990001)


async def _user() -> tuple[User, MediaProject]:
    await init_db()
    async with SessionLocal() as session:
        user = User(telegram_id=next(_USER_IDS), username="studio-assets")
        session.add(user)
        await session.flush()
        project = MediaProject(user_id=user.id, chat_id=user.telegram_id, name="Asset Test")
        session.add(project)
        await session.commit()
        await session.refresh(user)
        await session.refresh(project)
        session.expunge_all()
        return user, project


@pytest.mark.asyncio
async def test_asset_service_ingests_image_and_audio_into_safe_project_storage(tmp_path: Path) -> None:
    user, project = await _user()
    image = tmp_path / "unsafe name .. cover.jpg"
    audio = tmp_path / "voice.mp3"
    _ffmpeg("-f", "lavfi", "-i", "color=c=blue:s=320x240", "-frames:v", "1", str(image))
    _ffmpeg("-f", "lavfi", "-i", "sine=frequency=880:duration=1.2", "-c:a", "libmp3lame", str(audio))

    settings = Settings(project_dir=tmp_path / "projects", render_temp_dir=tmp_path / "tmp")
    service = AssetService(settings=settings)

    cover = await service.ingest_file(
        image,
        user_id=user.id,
        project_id=project.id,
        declared_type="image",
        source_type="telegram",
        mime_type="image/jpeg",
        telegram_file_id="photo-file-id",
    )
    sound = await service.ingest_file(
        audio,
        user_id=user.id,
        project_id=project.id,
        declared_type="audio",
        source_type="telegram",
        mime_type="audio/mpeg",
        telegram_file_id="audio-file-id",
    )

    assert cover.asset_type == "image"
    assert cover.width == 320
    assert cover.height == 240
    assert Path(cover.local_path).name == "source.jpg"
    assert (tmp_path / "projects" / str(project.id) / "assets") in Path(cover.local_path).parents
    assert sound.asset_type == "audio"
    assert sound.duration is not None and sound.duration > 1.0
    assert Path(sound.local_path).name == "source.mp3"

    async with SessionLocal() as session:
        stored = await session.get(MediaAsset, sound.id)
        assert stored is not None
        assert stored.telegram_file_id == "audio-file-id"


@pytest.mark.asyncio
async def test_asset_service_rejects_arbitrary_document_before_storage(tmp_path: Path) -> None:
    user, project = await _user()
    payload = tmp_path / "payload.txt"
    payload.write_text("not media", encoding="utf-8")
    settings = Settings(project_dir=tmp_path / "projects", render_temp_dir=tmp_path / "tmp")
    service = AssetService(settings=settings)

    with pytest.raises(ValueError, match="Unsupported media extension"):
        await service.ingest_file(
            payload,
            user_id=user.id,
            project_id=project.id,
            source_type="telegram",
            mime_type="text/plain",
        )
    assert not (tmp_path / "projects" / str(project.id)).exists()
