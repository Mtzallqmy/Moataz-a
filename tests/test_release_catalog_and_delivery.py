from __future__ import annotations

import time
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi import FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient

from app.bot import features
from app.config import Settings
from app.db import JobStatus
from app.services import artifact_delivery, github_releases
from app.services.artifact_urls import artifact_target
from app.services.downloader import MediaInfo

MIB = 1024 * 1024


@pytest.mark.parametrize(('url', 'tag', 'root'), [
    ('https://github.com/owner/repository', None, True),
    ('https://github.com/owner/repository/releases', None, False),
    ('https://github.com/owner/repository/releases/latest', None, False),
    ('https://github.com/owner/repository/releases/tag/v3.1', 'v3.1', False),
    ('https://github.com/owner/repository/releases/tag/build%2F2026', 'build/2026', False),
])
def test_release_references(url, tag, root):
    ref = github_releases.release_reference(url)
    assert ref and ref.tag == tag and ref.repository_root == root
    assert ref.owner == 'owner' and ref.repo == 'repository'


@pytest.mark.parametrize('url', [
    'https://github.com/owner/repo/releases/download/v1/a.apk',
    'https://github.com/owner/repo/blob/main/README.md',
    'https://example.com/owner/repo/releases/latest',
    'https://github.com/owner/repo/releases/tag/..',
])
def test_other_urls_do_not_open_release_catalog(url):
    assert github_releases.release_reference(url) is None


def test_encoded_slash_in_release_tag():
    target = artifact_target('https://github.com/owner/repo/releases/download/build%2F2026/app.apk')
    assert target.url.endswith('/build%2F2026/app.apk')
    assert target.filename == 'app.apk'


def _sample_catalog():
    ref = github_releases.ReleaseReference('owner', 'repo', repository_root=True)
    assets = [
        {'name': 'app-arm64.apk', 'state': 'uploaded', 'size': 129*MIB, 'browser_download_url': 'https://github.com/owner/repo/releases/download/v2/app-arm64.apk'},
        {'name': 'sources.zip', 'state': 'uploaded', 'size': 32*MIB, 'browser_download_url': 'https://github.com/owner/repo/releases/download/v2/sources.zip'},
        {'name': 'large.zip', 'state': 'uploaded', 'size': 205*MIB, 'browser_download_url': 'https://github.com/owner/repo/releases/download/v2/large.zip'},
        {'name': 'elsewhere.zip', 'state': 'uploaded', 'size': 1, 'browser_download_url': 'https://github.com/other/repo/releases/download/v2/elsewhere.zip'},
    ]
    return github_releases._catalog(ref, {'tag_name': 'v2', 'name': 'Version 2'}, assets)


def test_catalog_lists_downloadable_binaries_and_source():
    catalog = _sample_catalog()
    assert [asset.name for asset in catalog.assets] == ['app-arm64.apk', 'sources.zip', 'large.zip', 'Source code (ZIP)', 'Source code (TAR.GZ)', 'Repository (HEAD ZIP)']
    text, markup = features._catalog_markup('menuid', catalog, 0, 'ar')
    assert '129.0 MB' in text and '200.0 MB' in text and 'app-arm64.apk' in text
    assert markup.inline_keyboard[0][0].callback_data == 'ghasset:menuid:0'
    assert markup.inline_keyboard[1][0].callback_data == 'ghasset:menuid:1'
    assert markup.inline_keyboard[2][0].url == catalog.assets[2].url


async def test_release_api_paginates_assets(monkeypatch):
    github_releases._cache.clear()
    ref = github_releases.ReleaseReference('test', 'many')
    rows = [{'name': f'file-{i}.zip', 'size': i, 'browser_download_url': f'https://github.com/test/many/releases/download/v3/file-{i}.zip'} for i in range(110)]
    requests = []

    async def fake_json(_session, path, *, params=None):
        requests.append((path, params))
        if params is None:
            return {'id': 1, 'tag_name': 'v3', 'assets': rows[:100]}
        return rows[(params['page']-1)*100:params['page']*100]

    monkeypatch.setattr(github_releases, '_github_json', fake_json)
    catalog = await github_releases.fetch_release_catalog(ref)
    assert len(catalog.assets) == 112
    assert len(requests) == 3
    assert await github_releases.fetch_release_catalog(ref) is catalog
    assert len(requests) == 3
    github_releases._cache.clear()


async def test_catalog_selection_is_bound_to_user_and_chat(monkeypatch):
    catalog = _sample_catalog()
    token = 'test-menu'
    features._catalog_menus[token] = features._CatalogMenu(123, 456, time.monotonic()+100, catalog)
    calls = []
    answers = []
    message = SimpleNamespace(chat=SimpleNamespace(id=456))

    async def callback_answer(text='', **kwargs):
        answers.append((text, kwargs))

    callback = SimpleNamespace(message=message, data=f'ghasset:{token}:0', answer=callback_answer)

    async def analyze(message, user, url, intent):
        calls.append((url, intent))

    async def other_user(_callback):
        return SimpleNamespace(telegram_id=124, language='ar')

    async def owner(_callback):
        return SimpleNamespace(telegram_id=123, language='ar')

    async def allow(_key):
        return True

    monkeypatch.setattr(features, '_analyze_one', analyze)
    monkeypatch.setattr(features, '_get_callback_user', other_user)
    monkeypatch.setattr(features.telegram_analyze_limiter, 'allow', allow)
    await features.select_release_asset(callback)
    assert not calls and answers[-1][1]['show_alert']
    monkeypatch.setattr(features, '_get_callback_user', owner)
    await features.select_release_asset(callback)
    assert calls == [(catalog.assets[0].url, 'artifact_selected')]
    features._catalog_menus.pop(token)


async def test_selected_129_mb_file_is_queued(monkeypatch):
    edits, queued = [], []

    async def edit(text, **kwargs):
        edits.append((text, kwargs))

    async def enqueue(job_id, quality):
        queued.append((job_id, quality))

    monkeypatch.setattr(features, 'queue_existing_job', enqueue)
    info = MediaInfo('app.apk', None, None, None, 'github', file_size=129*MIB)
    job = SimpleNamespace(id=3, source_url='https://github.com/owner/repo/releases/download/v1/app.apk')
    await features._show_analysis(None, SimpleNamespace(edit_text=edit), job, info, 'artifact_selected', 'ar')
    assert queued == [(3, 'best')]
    assert 'رابط تحميل مؤقت' in edits[0][0]


def _settings(tmp_path: Path):
    return Settings(_env_file=None, bot_token='test-signing-key', download_dir=tmp_path, artifact_public_base_url='https://bot.example.com', max_file_size_mb=32)


def test_independent_200_mb_cap(tmp_path):
    settings = _settings(tmp_path)
    assert settings.artifact_download_limit_bytes == 200*MIB
    assert settings.max_file_size_bytes == 32*MIB


def test_signed_link_bound_to_job_and_expiration(tmp_path):
    settings = _settings(tmp_path)
    link = artifact_delivery.artifact_download_link(settings, 22, now=1000)
    assert link and link.startswith('https://bot.example.com/files/artifacts/22?')
    params = parse_qs(urlsplit(link).query)
    expires, token = int(params['expires'][0]), params['token'][0]
    assert artifact_delivery.valid_artifact_token(settings, 22, expires, token, now=1000)
    assert not artifact_delivery.valid_artifact_token(settings, 23, expires, token, now=1000)
    assert not artifact_delivery.valid_artifact_token(settings, 22, expires, 'é'*64, now=1000)
    assert not artifact_delivery.valid_artifact_token(settings, 22, expires, token, now=expires+1)


def _artifact_job(tmp_path):
    path = tmp_path / '8' / 'artifact' / 'sample.apk'
    path.parent.mkdir(parents=True)
    path.write_bytes(b'0123456789')
    job = SimpleNamespace(id=8, status=JobStatus.COMPLETED.value, platform='github', output_path=str(path), chat_id=5, file_size=10)
    return job, path


def test_download_path_not_other_jobs_or_non_artifacts(tmp_path):
    settings = _settings(tmp_path)
    job, path = _artifact_job(tmp_path)
    assert artifact_delivery.artifact_file_path(settings, job) == path
    job.platform = 'youtube'
    with pytest.raises(HTTPException):
        artifact_delivery.artifact_file_path(settings, job)
    job.platform = 'github'
    job.id = 9
    with pytest.raises(HTTPException):
        artifact_delivery.artifact_file_path(settings, job)


async def test_signed_file_endpoint_supports_range_and_rejects_invalid_token(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    job, _ = _artifact_job(tmp_path)

    class Session:
        async def get(self, _model, job_id):
            return job if job_id == job.id else None

    @asynccontextmanager
    async def session_local():
        yield Session()

    monkeypatch.setattr(artifact_delivery, 'SessionLocal', session_local)
    monkeypatch.setattr(artifact_delivery, 'get_settings', lambda: settings)
    app = FastAPI()
    app.include_router(artifact_delivery.router)
    link = artifact_delivery.artifact_download_link(settings, job.id)
    async with AsyncClient(transport=ASGITransport(app=app), base_url='https://bot.example.com') as client:
        response = await client.get(link, headers={'Range': 'bytes=2-5'})
        assert response.status_code == 206 and response.content == b'2345'
        assert 'sample.apk' in response.headers['content-disposition']
        assert (await client.get(link.replace('token=', 'token=bad'))).status_code == 404


async def test_worker_completes_large_artifact_with_link_without_upload(tmp_path, monkeypatch):
    from unittest.mock import AsyncMock

    from app import worker
    from app.db import Base, DownloadJob, MediaMetadata, SessionLocal, User, engine

    monkeypatch.setattr(worker.settings, 'download_dir', tmp_path)
    monkeypatch.setattr(worker.settings, 'artifact_public_base_url', 'https://bot.example.com')
    monkeypatch.setattr(worker.settings, 'max_file_size_mb', 32)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)
        await connection.run_sync(Base.metadata.create_all)
    async with SessionLocal() as session:
        user = User(telegram_id=888)
        session.add(user)
        await session.flush()
        url = 'https://github.com/owner/repo/releases/download/v1/app.apk'
        job = DownloadJob(user_id=user.id, chat_id=888, source_url=url, status=JobStatus.QUEUED.value, platform='github', selected_quality='best')
        session.add(job)
        await session.flush()
        job_id = job.id
        session.add(MediaMetadata(job_id=job_id, normalized_url=url))
        await session.commit()
    output = tmp_path / str(job_id) / 'artifact' / 'app.apk'
    output.parent.mkdir(parents=True)
    with output.open('wb') as stream:
        stream.truncate(129*MIB)
    monkeypatch.setattr(worker, '_download_with_retries', AsyncMock(return_value=output))
    monkeypatch.setattr(worker, '_update_worker', AsyncMock())
    monkeypatch.setattr(worker, '_record_backend_route', AsyncMock())
    uploads = AsyncMock(side_effect=AssertionError('Large artifacts must use link delivery'))
    monkeypatch.setattr(worker, '_upload_with_retries', uploads)
    edited = AsyncMock()
    monkeypatch.setattr(worker, '_edit_status', edited)
    await worker.process_download(job_id)
    uploads.assert_not_called()
    async with SessionLocal() as session:
        job = await session.get(DownloadJob, job_id)
        assert job.status == JobStatus.COMPLETED.value
        assert job.file_size == 129*MIB
        assert job.output_path == str(output)
    markup = edited.await_args.kwargs['markup']
    assert markup.inline_keyboard[0][0].url.startswith(f'https://bot.example.com/files/artifacts/{job_id}?')
    assert markup.inline_keyboard[1][0].url == url
