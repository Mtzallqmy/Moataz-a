import asyncio
from unittest.mock import AsyncMock

import pytest

from app.db import Base, DownloadJob, JobStatus, SessionLocal, User, engine
from app.errors import CancelledError, ErrorCode, classify_error
from app.queue import InlineJobManager


@pytest.mark.parametrize('message', [
    'HTTP Error 403: https://example.org/cancel/video',
    'Connection reset: request cancelled by upstream',
])
def test_upstream_cancellation_text_does_not_mean_user_cancelled(message):
    assert classify_error(RuntimeError(message)).code != ErrorCode.CANCELLED


def test_real_cancellation_types_are_preserved():
    assert classify_error(asyncio.CancelledError()).code == ErrorCode.CANCELLED
    assert classify_error(CancelledError('Download cancelled')).code == ErrorCode.CANCELLED


@pytest.mark.parametrize('user_cancelled', [False, True])
async def test_shutdown_does_not_turn_download_into_user_cancellation(tmp_path, monkeypatch, user_cancelled):
    import app.worker as worker

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    async with SessionLocal() as session:
        user = User(telegram_id=778)
        session.add(user)
        await session.flush()
        job = DownloadJob(user_id=user.id, chat_id=0, source_url='https://youtu.be/example',
                          selected_quality='audio', status=JobStatus.QUEUED.value)
        session.add(job)
        await session.commit()
        job_id, user_id = job.id, user.id
    started = asyncio.Event()
    cleaned = asyncio.Event()

    async def download(*args, **kwargs):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaned.set()

    monkeypatch.setattr(worker.settings, 'download_dir', tmp_path)
    monkeypatch.setattr(worker, '_download_with_retries', download)
    monkeypatch.setattr(worker, '_record_backend_route', AsyncMock())
    notify = AsyncMock()
    monkeypatch.setattr(worker, '_edit_status', notify)

    async def lookup(_):
        return user_id

    manager = InlineJobManager(runner=worker.process_download, user_lookup=lookup,
                               global_limit=1, per_user_limit=1)
    await manager.start()
    await manager.enqueue(job_id)
    await asyncio.wait_for(started.wait(), 2)
    if user_cancelled:
        from app.jobs import set_job_status
        await set_job_status(job_id, JobStatus.CANCELLED, error='CANCELLED')
    await asyncio.wait_for(manager.shutdown(), 2)
    assert cleaned.is_set()
    assert manager.active_job_ids == ()
    async with SessionLocal() as session:
        saved = await session.get(DownloadJob, job_id)
        if user_cancelled:
            assert saved.status == JobStatus.CANCELLED.value
            assert saved.error == 'CANCELLED'
            notify.assert_not_awaited()
        else:
            assert saved.status == JobStatus.FAILED.value
            assert saved.error.startswith('INTERRUPTED:')
            markup = notify.await_args.kwargs['markup']
            assert markup.inline_keyboard[0][0].callback_data == f'retry:{job_id}'


async def test_cancelled_job_can_be_retried_only_when_requested(monkeypatch):
    import app.queue as queue
    from app.services.job_service import queue_existing_job

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    async with SessionLocal() as session:
        user = User(telegram_id=779)
        session.add(user)
        await session.flush()
        job = DownloadJob(user_id=user.id, chat_id=0, source_url='https://youtu.be/example',
                          selected_quality='audio', status=JobStatus.CANCELLED.value,
                          error='CANCELLED')
        session.add(job)
        await session.commit()
        job_id = job.id
    enqueue = AsyncMock()
    monkeypatch.setattr(queue, 'enqueue_download', enqueue)
    enqueue.assert_not_called()
    await queue_existing_job(job_id, 'audio')
    enqueue.assert_awaited_once_with(job_id, priority=0)
    async with SessionLocal() as session:
        job = await session.get(DownloadJob, job_id)
        assert job.status == JobStatus.QUEUED.value
        assert job.error is None
