from types import SimpleNamespace

import pytest

from app.services.projects import ProjectAssetItem
from app.services.studio_session import (
    StudioPhase,
    classify_project_assets,
    project_summary_text,
    recent_asset_context,
    transition_phase,
)


def _item(
    asset_id: int,
    kind: str,
    *,
    role: str = "main",
    name: str = "asset",
    duration: float = 0,
) -> ProjectAssetItem:
    return ProjectAssetItem(
        link=SimpleNamespace(role=role),
        asset=SimpleNamespace(
            id=asset_id,
            asset_type=kind,
            duration=duration,
            local_path=f"/safe/{asset_id}",
            metadata_json=f'{{"original_name":"{name}"}}',
        ),
    )


def test_project_media_is_classified_before_editing() -> None:
    items = [
        _item(1, "video", name="main.mp4", duration=12),
        _item(2, "video", name="broll.mp4", duration=8),
        _item(3, "image", name="product.jpg"),
        _item(4, "image", role="logo", name="brand.png"),
        _item(5, "audio", role="music", name="bed.mp3"),
        _item(6, "voice", role="voice", name="voice.ogg"),
        _item(7, "subtitle", name="captions.srt"),
    ]
    summary = classify_project_assets(items)
    assert summary.videos == 2
    assert summary.images == 1
    assert summary.logos == 1
    assert summary.music == 1
    assert summary.voice_audio == 1
    assert summary.subtitles == 1
    assert summary.video_duration == 20
    assert summary.total == 7


def test_recent_asset_context_is_explicit_and_reports_ambiguity() -> None:
    items = [_item(11, "image", name="one.jpg"), _item(12, "image", name="two.jpg")]
    context = recent_asset_context(items, 12)
    assert context == {
        "asset_id": 12,
        "asset_type": "image",
        "role": "main",
        "name": "two.jpg",
        "type_index": 2,
        "ambiguous_type_count": 2,
    }


def test_project_summary_exposes_phase_timeline_and_edit_settings() -> None:
    project = SimpleNamespace(id=43, aspect_ratio="9:16")
    timeline = {
        "revision": 7,
        "options": {"style_preset": "reels-fast", "audio_mode": "mix_audio"},
        "tracks": [
            {"kind": "visual", "clips": [{"start": 0, "duration": 30}]},
            {"kind": "subtitle", "clips": [{"start": 0, "duration": 3}]},
        ],
    }
    text = project_summary_text(
        project,
        [_item(1, "video", duration=30)],
        timeline,
        phase=StudioPhase.AWAITING_FEEDBACK,
    )
    assert "Project #43" in text
    assert "AWAITING_FEEDBACK" in text
    assert "Timeline: 00:00 → 00:30" in text
    assert "Style: reels-fast" in text
    assert "Captions: مفعّل" in text
    assert "Revision: 7" in text


def test_studio_phase_machine_rejects_overlapping_work() -> None:
    assert (
        transition_phase(StudioPhase.NEW, StudioPhase.COLLECTING_MEDIA)
        == StudioPhase.COLLECTING_MEDIA
    )
    assert (
        transition_phase(
            StudioPhase.COLLECTING_MEDIA, StudioPhase.READY_FOR_INSTRUCTIONS
        )
        == StudioPhase.READY_FOR_INSTRUCTIONS
    )
    with pytest.raises(ValueError, match="Invalid Studio phase transition"):
        transition_phase(StudioPhase.COLLECTING_MEDIA, StudioPhase.FINAL_RENDERING)
