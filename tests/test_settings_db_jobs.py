import pytest

from app.config import Settings
from app.db import Base, DownloadJob, JobStatus, MediaMetadata, SessionLocal, User, engine
from app.jobs import can_transition
from app.operations import reconcile_stale_jobs


def test_settings_require_no_redis_webhook_or_cookie_fields():
    settings = Settings(_env_file=None)
    assert not hasattr(settings, "redis_url")
    assert not hasattr(settings, "queue_backend")
    assert not hasattr(settings, "webhook_secret")
    assert not hasattr(settings, "ytdlp_cookies_file")


def test_database_url_normalization():
    assert Settings(_env_file=None, database_url="postgres://u:p@db/x").database_url == "postgresql+asyncpg://u:p@db/x"
    assert Settings(_env_file=None, database_url="postgresql://u:p@db/x").database_url == "postgresql+asyncpg://u:p@db/x"


def test_settings_import_has_no_filesystem_side_effect(tmp_path):
    target = tmp_path / "never-created"
    settings = Settings(_env_file=None, download_dir=target)
    assert settings.download_dir == target
    assert not target.exists()


def test_stale_telegram_local_api_variable_is_ignored():
    settings = Settings(_env_file=None, telegram_local_api_url="http://broken.local")
    assert not hasattr(settings, "telegram_local_api_url")


def test_status_transition_matrix_blocks_invalid_terminal_restarts():
    assert can_transition(JobStatus.READY.value, JobStatus.QUEUED.value)
    assert can_transition(JobStatus.DOWNLOADING.value, JobStatus.MERGING.value)
    assert can_transition(JobStatus.UPLOADING.value, JobStatus.COMPLETED.value)
    assert not can_transition(JobStatus.COMPLETED.value, JobStatus.DOWNLOADING.value)


@pytest.mark.asyncio
async def test_stale_job_recovery_requeues_queued_and_fails_process_bound():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    async with SessionLocal() as session:
        user = User(telegram_id=99, username="tester")
        session.add(user)
        await session.flush()
        queued = DownloadJob(user_id=user.id, chat_id=1, source_url="https://example.com/q", status="QUEUED")
        downloading = DownloadJob(user_id=user.id, chat_id=1, source_url="https://example.com/d", status="DOWNLOADING")
        ready = DownloadJob(user_id=user.id, chat_id=1, source_url="https://example.com/r", status="READY")
        session.add_all([queued, downloading, ready])
        await session.flush()
        for job in (queued, downloading, ready):
            session.add(MediaMetadata(job_id=job.id, normalized_url=f"https://example.com/{job.id}"))
        await session.commit()
        ids = queued.id, downloading.id, ready.id

    result = await reconcile_stale_jobs()
    assert result.requeue_ids == (ids[0],)
    assert result.failed_ids == (ids[1],)
    async with SessionLocal() as session:
        q = await session.get(DownloadJob, ids[0])
        d = await session.get(DownloadJob, ids[1])
        r = await session.get(DownloadJob, ids[2])
        assert q.status == "QUEUED"
        assert d.status == "FAILED"
        assert "INTERRUPTED" in d.error
        assert r.status == "READY"
