from __future__ import annotations

import itertools
import subprocess
from pathlib import Path

import pytest

from app.config import Settings
from app.db import MediaAssetAnalysis, SessionLocal, User, init_db
from app.services.assets import AssetService
from app.services.media_intelligence import (
    MediaIntelligenceService,
    ProviderVisionAnalyzer,
)
from app.services.openai_compatible import ChatReply
from app.services.projects import ProjectService

_IDS = itertools.count(9_600_000)


def _media(path: Path) -> Path:
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=red:s=160x90:d=1",
            "-f",
            "lavfi",
            "-i",
            "color=c=blue:s=160x90:d=1",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=0.6",
            "-f",
            "lavfi",
            "-i",
            "anullsrc=r=48000:cl=mono:d=0.8",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=660:duration=0.6",
            "-filter_complex",
            "[0:v][1:v]concat=n=2:v=1:a=0[v];[2:a][3:a][4:a]concat=n=3:v=0:a=1[a]",
            "-map",
            "[v]",
            "-map",
            "[a]",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-shortest",
            str(path),
        ],
        check=True,
        capture_output=True,
    )
    return path


class FakeTranscriber:
    def __init__(self) -> None:
        self.calls = 0

    async def transcribe(self, source: Path):
        self.calls += 1
        return {
            "language": "ar",
            "text": "مرحبا بالعالم",
            "segments": [{"start": 0, "end": 0.6, "text": "مرحبا بالعالم"}],
            "words": [
                {"start": 0, "end": 0.25, "word": "مرحبا"},
                {"start": 0.25, "end": 0.6, "word": "بالعالم"},
            ],
        }


class FakeVision:
    cache_key = "vision-model-v1"

    def __init__(self) -> None:
        self.calls = 0

    async def describe(self, frames: list[Path]):
        self.calls += 1
        return [
            {
                "description": f"scene {index}",
                "focus": {"x": 0.25, "y": 0.75},
            }
            for index, _ in enumerate(frames)
        ]


class FakeVisionRegistry:
    def __init__(self) -> None:
        self.messages = []

    async def chat(self, provider_id, model, messages, *, max_reply_chars=None):
        self.messages.append((provider_id, model, messages, max_reply_chars))
        return ChatReply(
            text='[{"description":"منتج على الطاولة","focus":{"x":2,"y":-1}}]',
            model=model,
        )


@pytest.mark.asyncio
async def test_media_intelligence_detects_scenes_silence_and_caches_transcription(
    tmp_path: Path,
) -> None:
    await init_db()
    settings = Settings(project_dir=tmp_path / "projects", render_temp_dir=tmp_path / "tmp")
    async with SessionLocal() as session:
        user = User(telegram_id=next(_IDS))
        session.add(user)
        await session.commit()
        await session.refresh(user)
        session.expunge(user)
    projects = ProjectService(settings)
    project = await projects.create_project(user_id=user.id, chat_id=user.telegram_id)
    source = _media(tmp_path / "intelligence.mp4")
    asset = await AssetService(settings).ingest_file(
        source,
        user_id=user.id,
        project_id=project.id,
        declared_type="video",
    )
    await projects.add_asset(project.id, asset.id, user_id=user.id)
    transcriber = FakeTranscriber()
    service = MediaIntelligenceService(settings, transcriber=transcriber)

    first = await service.analyze_asset(
        asset.id, user_id=user.id, project_id=project.id
    )
    second = await service.analyze_asset(
        asset.id, user_id=user.id, project_id=project.id
    )

    assert first.status == "COMPLETED" and first.cached is False
    assert second.cached is True
    assert transcriber.calls == 1
    assert first.payload["transcription"]["words"][0]["word"] == "مرحبا"
    assert first.payload["audio"]["silence"]
    assert len(first.payload["scenes"]) >= 2
    assert all(Path(scene["thumbnail"]).is_file() for scene in first.payload["scenes"])
    async with SessionLocal() as session:
        row = await session.get(MediaAssetAnalysis, asset.id)
        assert row is not None and row.status == "COMPLETED"


@pytest.mark.asyncio
async def test_media_intelligence_enforces_project_ownership(tmp_path: Path) -> None:
    await init_db()
    settings = Settings(project_dir=tmp_path / "projects", render_temp_dir=tmp_path / "tmp")
    async with SessionLocal() as session:
        owner = User(telegram_id=next(_IDS))
        stranger = User(telegram_id=next(_IDS))
        session.add_all([owner, stranger])
        await session.commit()
        session.expunge_all()
    project = await ProjectService(settings).create_project(
        user_id=owner.id, chat_id=owner.telegram_id
    )
    asset = await AssetService(settings).ingest_file(
        _media(tmp_path / "owned.mp4"),
        user_id=owner.id,
        project_id=project.id,
        declared_type="video",
    )
    await ProjectService(settings).add_asset(project.id, asset.id, user_id=owner.id)
    with pytest.raises(LookupError):
        await MediaIntelligenceService(settings).analyze_asset(
            asset.id,
            user_id=stranger.id,
            project_id=project.id,
        )


@pytest.mark.asyncio
async def test_provider_vision_is_safe_structured_and_clamps_focus(tmp_path: Path) -> None:
    frame = tmp_path / "frame.jpg"
    frame.write_bytes(b"jpeg-fixture")
    registry = FakeVisionRegistry()
    analyzer = ProviderVisionAnalyzer(
        registry,  # type: ignore[arg-type]
        provider_id="configured-provider",
        model="vision-model",
        allowed_root=tmp_path,
    )
    result = await analyzer.describe([frame])
    assert result == [
        {
            "description": "منتج على الطاولة",
            "focus": {"x": 1.0, "y": 0.0},
        }
    ]
    assert registry.messages[0][0:2] == ("configured-provider", "vision-model")
    content = registry.messages[0][2][0]["content"]
    assert content[1]["image_url"]["url"].startswith("data:image/jpeg;base64,")


@pytest.mark.asyncio
async def test_vision_enrichment_is_persisted_and_reused(tmp_path: Path) -> None:
    await init_db()
    settings = Settings(
        project_dir=tmp_path / "projects", render_temp_dir=tmp_path / "tmp"
    )
    async with SessionLocal() as session:
        user = User(telegram_id=next(_IDS))
        session.add(user)
        await session.commit()
        await session.refresh(user)
        session.expunge(user)
    projects = ProjectService(settings)
    project = await projects.create_project(user_id=user.id, chat_id=user.telegram_id)
    asset = await AssetService(settings).ingest_file(
        _media(tmp_path / "vision.mp4"),
        user_id=user.id,
        project_id=project.id,
        declared_type="video",
    )
    await projects.add_asset(project.id, asset.id, user_id=user.id)
    service = MediaIntelligenceService(settings)
    vision = FakeVision()
    first = await service.enrich_project_vision(
        project.id, user_id=user.id, vision=vision  # type: ignore[arg-type]
    )
    second = await service.enrich_project_vision(
        project.id, user_id=user.id, vision=vision  # type: ignore[arg-type]
    )
    assert vision.calls == 1
    assert first["assets"][0]["vision_used"] is True
    assert second["assets"][0]["vision_cache_key"] == "vision-model-v1"
    stored = await service.get(asset.id, user_id=user.id)
    assert stored is not None
    assert stored.payload["scenes"][0]["description"].startswith("scene")
