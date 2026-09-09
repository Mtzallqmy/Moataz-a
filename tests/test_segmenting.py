from pathlib import Path

import pytest

from app.services import segmenting
from app.services.media import MediaProbe


@pytest.mark.asyncio
async def test_video_split_builds_consecutive_30_second_parts(monkeypatch, tmp_path):
    source = tmp_path / "source.webm"
    source.write_bytes(b"media")

    async def fake_probe(path):
        assert path == source
        return MediaProbe(duration=95.0, has_video=True, has_audio=True, size_bytes=5)

    captured = []

    async def fake_run(*args, **kwargs):
        captured.extend(args)
        pattern = Path(args[-1])
        for index in range(4):
            Path(str(pattern).replace("%03d", f"{index:03d}")).write_bytes(b"part")
        return b"", b""

    monkeypatch.setattr(segmenting, "probe_media_file", fake_probe)
    monkeypatch.setattr(segmenting, "_run_process", fake_run)

    parts = await segmenting.split_media(source, 30)

    assert len(parts) == 4
    assert all(part.suffix == ".mp4" for part in parts)
    assert "-segment_time" in captured
    assert captured[captured.index("-segment_time") + 1] == "30"
    assert "-force_key_frames" in captured
    assert "libx264" in captured


@pytest.mark.asyncio
async def test_audio_split_outputs_mp3_parts(monkeypatch, tmp_path):
    source = tmp_path / "source.mp3"
    source.write_bytes(b"audio")

    async def fake_probe(path):
        assert path == source
        return MediaProbe(duration=121.0, has_video=False, has_audio=True, size_bytes=5)

    captured = []

    async def fake_run(*args, **kwargs):
        captured.extend(args)
        pattern = Path(args[-1])
        for index in range(3):
            Path(str(pattern).replace("%03d", f"{index:03d}")).write_bytes(b"part")
        return b"", b""

    monkeypatch.setattr(segmenting, "probe_media_file", fake_probe)
    monkeypatch.setattr(segmenting, "_run_process", fake_run)

    parts = await segmenting.split_media(source, 60)

    assert len(parts) == 3
    assert all(part.suffix == ".mp3" for part in parts)
    assert "libmp3lame" in captured
    assert captured[captured.index("-segment_time") + 1] == "60"


def test_split_rejects_unbounded_message_floods():
    probe = MediaProbe(duration=4_000.0, has_video=True, has_audio=True, size_bytes=1)
    with pytest.raises(ValueError, match="limit"):
        segmenting._normalize_range(probe, start=0, end=None, segment_seconds=30)


def test_split_range_can_target_part_of_media():
    probe = MediaProbe(duration=600.0, has_video=True, has_audio=True, size_bytes=1)
    start, end, count = segmenting._normalize_range(
        probe,
        start=120,
        end=275,
        segment_seconds=30,
    )
    assert start == 120
    assert end == 275
    assert count == 6
