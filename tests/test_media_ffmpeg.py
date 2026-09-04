import asyncio

import pytest

from app.services.media import fit_media_for_upload, probe_media_file


@pytest.mark.asyncio
async def test_adaptive_upload_pipeline_with_real_ffmpeg(tmp_path):
    source = tmp_path / "source.mp4"
    process = await asyncio.create_subprocess_exec(
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-f",
        "lavfi",
        "-i",
        "testsrc=size=1280x720:rate=24",
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=1000",
        "-t",
        "2",
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-c:a",
        "aac",
        str(source),
    )
    assert await process.wait() == 0

    probe = await probe_media_file(source)
    assert probe.has_video is True
    assert probe.has_audio is True
    assert probe.duration > 1

    limit = max(40_000, int(source.stat().st_size * 0.75))
    output = await fit_media_for_upload(source, limit, tmp_path, attempts=2)
    assert output.stat().st_size <= limit
    assert output.suffix == ".mp4"
