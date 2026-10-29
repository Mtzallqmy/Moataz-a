import asyncio
import json
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from aiogram.exceptions import TelegramNetworkError
from aiogram.methods import EditMessageText

from app.db import Base, DownloadJob, JobStatus, MediaMetadata, SessionLocal, User, engine
from app.services.download_cache import cached_source, owned_file, remember_source
from app.services.media import probe_media_file


@pytest.fixture
async def saved_job(tmp_path, monkeypatch):
    import app.bot.advanced_media as advanced
    import app.worker as worker

    monkeypatch.setattr(advanced.settings, "download_dir", tmp_path)
    monkeypatch.setattr(worker.settings, "download_dir", tmp_path)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    async with SessionLocal() as session:
        user = User(telegram_id=505)
        session.add(user)
        await session.flush()
        job = DownloadJob(user_id=user.id, chat_id=0, source_url="https://youtu.be/example",
                          status=JobStatus.COMPLETED.value, duration=999, selected_quality="best")
        session.add(job)
        await session.flush()
        folder = tmp_path / str(job.id)
        folder.mkdir()
        source = folder / "video.mp4"
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                        "color=c=blue:s=160x90:d=3", "-f", "lavfi", "-i", "sine=frequency=440:duration=3",
                        "-c:v", "libx264", "-c:a", "aac", "-shortest", str(source)],
                       check=True, capture_output=True)
        job.output_path = str(source)
        session.add(MediaMetadata(job_id=job.id, normalized_url=job.source_url))
        await session.commit()
        return job.id, user.id, source


async def test_completed_file_can_be_cut_without_calling_downloader(saved_job, monkeypatch):
    import app.worker as worker
    from app.bot.advanced_media import _clone_ready_job

    job_id, user_id, original = saved_job
    clone, info = await _clone_ready_job(job_id, user_id, 0, local=True)
    assert info.duration == 3  # use the actual clip, not the original URL duration
    assert Path(clone.output_path) != original
    original.unlink()  # queued edit owns a separate file, survives original cleanup
    async with SessionLocal() as session:
        job = await session.get(DownloadJob, clone.id)
        job.status = JobStatus.QUEUED.value
        job.cut_start, job.cut_end = 0.5, 1.5
        await session.commit()
    download = AsyncMock(side_effect=AssertionError("must not redownload"))
    monkeypatch.setattr(worker.downloader, "download", download)
    await worker.process_download(clone.id)
    async with SessionLocal() as session:
        result = await session.get(DownloadJob, clone.id)
        assert result.status == JobStatus.COMPLETED.value, result.error
        probe = await probe_media_file(Path(result.output_path))
        assert 0.8 <= probe.duration <= 1.2
    download.assert_not_called()


async def test_local_clone_rejects_other_owner_and_expired_file(saved_job):
    from app.bot.advanced_media import _clone_ready_job

    job_id, user_id, original = saved_job
    with pytest.raises(LookupError):
        await _clone_ready_job(job_id, user_id + 1, 0, local=True)
    original.unlink()
    with pytest.raises(ValueError):
        await _clone_ready_job(job_id, user_id, 0, local=True)


async def test_cached_source_validates_quality_and_stays_inside_workspace(saved_job):
    _, _, source = saved_job
    remember_source(source.parent, source, "best")
    assert await cached_source(source.parent, "best") == source.resolve()
    assert await cached_source(source.parent, "audio") is None
    (source.parent / "download-source.json").write_text(json.dumps({"file": "../outside.mp4", "quality": "best"}))
    assert await cached_source(source.parent, "best") is None
    source.write_bytes(b"not a media file")
    remember_source(source.parent, source, "best")
    assert await cached_source(source.parent, "best") is None


def test_saved_media_symlink_cannot_escape_workspace(tmp_path):
    outside = tmp_path / "secret.mp4"
    outside.write_bytes(b"secret")
    job = tmp_path / "1"
    job.mkdir()
    link = job / "media.mp4"
    link.symlink_to(outside)
    with pytest.raises(ValueError):
        owned_file(link, job)


async def test_telegram_progress_failure_does_not_abort_job(monkeypatch):
    import app.worker as worker

    failure = TelegramNetworkError(method=EditMessageText(chat_id=1, message_id=1, text="x"), message="reset")
    monkeypatch.setattr(worker, "_send_status", AsyncMock(side_effect=failure))
    await worker._edit_status(1, "downloading")
    monkeypatch.setattr(worker, "_send_status", AsyncMock(side_effect=TimeoutError()))
    await worker._edit_status(1, "downloading")


async def test_completed_keyboard_has_local_cut():
    from app.bot.advanced_media import completed_reuse_keyboard

    callbacks = [b.callback_data for row in completed_reuse_keyboard(9).inline_keyboard for b in row]
    assert "reuse:9:localcut" in callbacks
    assert "reuse:9:localsplit" in callbacks


@pytest.mark.parametrize("start,end", [(float("nan"), 3), (0, float("inf")), (0, float("nan"))])
def test_cut_rejects_non_finite_times(start, end):
    from app.services.cutting import resolve_cut_plan

    with pytest.raises(ValueError):
        resolve_cut_plan(start=start, end=end, source_duration=60)


async def test_extractor_deadline_terminates_process(monkeypatch):
    import sys

    from app.config import Settings
    from app.services.download_process import run_download_process

    spawn = asyncio.create_subprocess_exec
    processes = []

    async def hanging_child(*args, **kwargs):
        process = await spawn(sys.executable, "-c", "import time; time.sleep(60)", **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", hanging_child)
    settings = Settings(_env_file=None).model_copy(update={"probe_timeout_seconds": 0.05})
    with pytest.warns(UserWarning, match="Pydantic serializer"), pytest.raises(TimeoutError, match="deadline"):
        await run_download_process(settings, "probe", "https://youtu.be/example")
    assert processes[0].returncode is not None


async def test_extractor_cancellation_terminates_process(monkeypatch):
    import sys
    import threading

    from app.config import Settings
    from app.errors import CancelledError
    from app.services.download_process import run_download_process

    spawn = asyncio.create_subprocess_exec
    event = threading.Event()
    processes = []

    async def hanging_child(*args, **kwargs):
        process = await spawn(sys.executable, "-c", "import time; time.sleep(60)", **kwargs)
        processes.append(process)
        event.set()
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", hanging_child)
    with pytest.raises(CancelledError):
        await run_download_process(Settings(_env_file=None), "probe", "https://youtu.be/example", cancel_event=event)
    assert processes[0].returncode is not None


async def test_real_child_preserves_error_classification():
    from app.config import Settings
    from app.errors import ErrorCode, MediaError
    from app.services.download_process import run_download_process

    with pytest.raises(MediaError) as error:
        await run_download_process(Settings(_env_file=None), "probe", "http://127.0.0.1/private")
    assert error.value.code == ErrorCode.INVALID_URL


async def test_local_retry_uses_original_source_even_after_output_path_changes(saved_job):
    import threading

    from app.bot.advanced_media import _clone_ready_job
    from app.worker import _download_with_retries

    job_id, user_id, _ = saved_job
    clone, _ = await _clone_ready_job(job_id, user_id, 0, local=True)
    saved = Path(clone.output_path)
    async with SessionLocal() as session:
        job = await session.get(DownloadJob, clone.id)
        job.output_path = str(saved.parent / "previous-cut.mp4")
        await session.commit()
    source = await _download_with_retries(clone.id, clone.source_url, "best", saved.parent, [], None, threading.Event())
    assert source == saved.resolve()


async def test_local_video_can_be_split_as_audio_without_network(saved_job):
    import threading

    from app.bot.advanced_media import _clone_ready_job
    from app.worker import _download_with_retries

    job_id, user_id, _ = saved_job
    clone, _ = await _clone_ready_job(job_id, user_id, 0, local=True)
    source = await _download_with_retries(clone.id, clone.source_url, "audio", Path(clone.output_path).parent,
                                          [], None, threading.Event())
    probe = await probe_media_file(source)
    assert probe.has_audio and not probe.has_video


def test_neon_url_is_compatible_with_asyncpg_and_verifies_tls():
    import ssl

    from sqlalchemy.pool import NullPool

    from app.config import Settings
    from app.db import database_engine_options

    url = Settings(_env_file=None, database_url="postgresql://u:p@ep-example-pooler.eu-central-1.aws.neon.tech/db?sslmode=require&channel_binding=require").database_url
    assert "sslmode" not in url and "channel_binding" not in url
    assert "ssl=require" in url
    options = database_engine_options(url)
    assert options["poolclass"] is NullPool
    assert options["connect_args"]["statement_cache_size"] == 0
    assert options["connect_args"]["ssl"].verify_mode == ssl.CERT_REQUIRED
    assert options["connect_args"]["ssl"].check_hostname
