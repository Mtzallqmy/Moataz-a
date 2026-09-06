import threading
from pathlib import Path

import pytest

from app.errors import CancelledError
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

    async def fake_run(*args, **kwargs):
        captured.extend(args)
        Path(args[-1]).write_bytes(b"output")
        return b"", b""

    monkeypatch.setattr(media, "_run_process", fake_run)
    output = await media.cut_media(source, 1, 3, mode=mode, source_duration=10)
    assert output.exists()
    joined = " ".join(captured)
    for token in required:
        assert token in captured
    assert forbidden not in joined
    assert "shell=True" not in joined


@pytest.mark.asyncio
async def test_cut_range_validation(tmp_path):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"x")
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
