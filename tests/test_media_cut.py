import threading
from pathlib import Path

import pytest

from app.errors import CancelledError, FFmpegError
from app.services import media


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "required", "forbidden"),
    [
        ("FAST", ("-c", "copy"), "libx264"),
        ("PRECISE", ("-c:v", "libx264", "-c:a", "aac"), "-c copy"),
    ],
)
async def test_cut_modes_build_safe_ffmpeg_arguments(monkeypatch, tmp_path, mode, required, forbidden):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source")
    captured = []

    async def fake_probe(path):
        return media.MediaProbe(duration=10, has_video=True, has_audio=True, size_bytes=path.stat().st_size)

    async def fake_run(*args, **kwargs):  # noqa: ARG001
        captured.extend(args)
        Path(args[-1]).write_bytes(b"output")
        return b"", b""

    monkeypatch.setattr(media, "probe_media_file", fake_probe)
    monkeypatch.setattr(media, "_run_process", fake_run)
    output = await media.cut_media(source, 1, 3, mode=mode, source_duration=10)
    assert output.exists()
    joined = " ".join(captured)
    for token in required:
        assert token in captured
    assert forbidden not in joined
    assert "shell=True" not in joined
    if mode == "PRECISE":
        assert "scale=trunc(iw/2)*2:trunc(ih/2)*2" in captured
        assert "yuv420p" in captured


@pytest.mark.asyncio
async def test_fast_cut_falls_back_to_precise_transcode(monkeypatch, tmp_path):
    source = tmp_path / "source.webm"
    source.write_bytes(b"source")
    calls = []

    async def fake_probe(path):
        return media.MediaProbe(duration=12, has_video=True, has_audio=True, size_bytes=path.stat().st_size)

    async def fake_run(*args, **kwargs):  # noqa: ARG001
        calls.append(args)
        if "copy" in args:
            raise FFmpegError("container cannot stream-copy this codec")
        Path(args[-1]).write_bytes(b"transcoded")
        return b"", b""

    monkeypatch.setattr(media, "probe_media_file", fake_probe)
    monkeypatch.setattr(media, "_run_process", fake_run)

    output = await media.cut_media(source, 2, 5, mode="FAST", source_duration=12)

    assert output.suffix == ".mp4"
    assert output.name.endswith(".precise.mp4")
    assert any("copy" in call for call in calls)
    assert any("libx264" in call for call in calls)


@pytest.mark.asyncio
async def test_precise_cut_uses_mpeg4_fallback_when_h264_fails(monkeypatch, tmp_path):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source")
    codecs = []

    async def fake_probe(path):
        return media.MediaProbe(duration=10, has_video=True, has_audio=False, size_bytes=path.stat().st_size)

    async def fake_run(*args, **kwargs):  # noqa: ARG001
        if "-c:v" in args:
            codec = args[args.index("-c:v") + 1]
            codecs.append(codec)
            if codec == "libx264":
                raise FFmpegError("encoder unavailable")
        Path(args[-1]).write_bytes(b"output")
        return b"", b""

    monkeypatch.setattr(media, "probe_media_file", fake_probe)
    monkeypatch.setattr(media, "_run_process", fake_run)

    output = await media.cut_media(source, 1, 4, mode="PRECISE", source_duration=10)

    assert output.exists()
    assert codecs == ["libx264", "mpeg4"]


@pytest.mark.asyncio
async def test_cut_uses_actual_downloaded_duration_with_rounding_tolerance(monkeypatch, tmp_path):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source")
    captured = []

    async def fake_probe(path):
        return media.MediaProbe(duration=9.8, has_video=True, has_audio=True, size_bytes=path.stat().st_size)

    async def fake_run(*args, **kwargs):  # noqa: ARG001
        captured.extend(args)
        Path(args[-1]).write_bytes(b"output")
        return b"", b""

    monkeypatch.setattr(media, "probe_media_file", fake_probe)
    monkeypatch.setattr(media, "_run_process", fake_run)

    await media.cut_media(source, 1, 10, mode="PRECISE", source_duration=10)

    duration_value = float(captured[captured.index("-t") + 1])
    assert duration_value == pytest.approx(8.8)


@pytest.mark.asyncio
async def test_cut_range_validation(monkeypatch, tmp_path):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"x")

    async def fake_probe(path):
        return media.MediaProbe(duration=10, has_video=True, has_audio=True, size_bytes=path.stat().st_size)

    monkeypatch.setattr(media, "probe_media_file", fake_probe)

    with pytest.raises(ValueError):
        await media.cut_media(source, -1, 2, source_duration=10)
    with pytest.raises(ValueError):
        await media.cut_media(source, 3, 2, source_duration=10)
    with pytest.raises(ValueError):
        await media.cut_media(source, 1, 12, source_duration=10)


@pytest.mark.asyncio
async def test_ffmpeg_cancellation_terminates_process(monkeypatch):
    terminated = {"value": False}

    class FakeStream:
        async def read(self, size):  # noqa: ARG002
            return b""

    class FakeProcess:
        returncode = None
        stdout = FakeStream()
        stderr = FakeStream()

        async def wait(self):
            if self.returncode is not None:
                return self.returncode
            await __import__("asyncio").sleep(10)
            return self.returncode or 0

        def terminate(self):
            self.returncode = -15
            terminated["value"] = True

        def kill(self):
            self.returncode = -9
            terminated["value"] = True

    async def fake_create(*args, **kwargs):  # noqa: ARG001
        return FakeProcess()

    monkeypatch.setattr(media.asyncio, "create_subprocess_exec", fake_create)
    event = threading.Event()
    event.set()
    with pytest.raises(CancelledError):
        await media._run_process("ffmpeg", "-version", timeout=2, cancel_event=event)
    assert terminated["value"] is True


@pytest.mark.asyncio
async def test_ffmpeg_failure_without_stderr_keeps_exit_signal(monkeypatch):
    class FakeStream:
        async def read(self, size):  # noqa: ARG002
            return b""

    class FakeProcess:
        returncode = -9
        stdout = FakeStream()
        stderr = FakeStream()

        async def wait(self):
            return self.returncode

    async def fake_create(*args, **kwargs):  # noqa: ARG001
        return FakeProcess()

    monkeypatch.setattr(media.asyncio, "create_subprocess_exec", fake_create)
    with pytest.raises(FFmpegError, match=r"SIGKILL.*no stderr diagnostics"):
        await media._run_process("ffmpeg", "-version", timeout=2)
