from __future__ import annotations

import threading
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from app.config import Settings
from app.errors import CancelledError, InvalidURLError
from app.services.artifact_urls import artifact_target, safe_filename
from app.services.download_backends.artifact_backend import ArtifactBackend
from app.services.download_backends.base import DownloadRequest
from app.services.download_backends.router import DownloadManager, PlatformDetector, ProviderRouter


@pytest.mark.parametrize(('url', 'target', 'name'), [
    ('https://github.com/owner/repo', 'https://github.com/owner/repo/archive/HEAD.zip', 'repo.zip'),
    ('https://github.com/owner/repo.git', 'https://github.com/owner/repo/archive/HEAD.zip', 'repo.zip'),
    ('https://github.com/owner/repo/releases/download/v1/app.apk', 'https://github.com/owner/repo/releases/download/v1/app.apk', 'app.apk'),
    ('https://github.com/owner/repo/releases/latest/download/app.apk', 'https://github.com/owner/repo/releases/latest/download/app.apk', 'app.apk'),
    ('https://github.com/owner/repo/releases/tag/v1', 'https://github.com/owner/repo/archive/refs/tags/v1.zip', 'repo-v1.zip'),
    ('https://github.com/owner/repo/blob/main/docs/a.txt?raw=true', 'https://raw.githubusercontent.com/owner/repo/main/docs/a.txt', 'a.txt'),
    ('https://github.com/owner/repo/blob/main/docs/a.txt', 'https://raw.githubusercontent.com/owner/repo/main/docs/a.txt', 'a.txt'),
    ('https://gitlab.com/group/repo', 'https://gitlab.com/api/v4/projects/group%2Frepo/repository/archive.zip', 'repo.zip'),
    ('https://gitlab.com/group/repo/-/blob/main/a.txt', 'https://gitlab.com/group/repo/-/raw/main/a.txt', 'a.txt'),
    ('https://huggingface.co/owner/model/blob/main/config.json', 'https://huggingface.co/owner/model/resolve/main/config.json', 'config.json'),
    ('https://files.pythonhosted.org/packages/a/b/example.whl', 'https://files.pythonhosted.org/packages/a/b/example.whl', 'example.whl'),
])
def test_public_file_links(url, target, name):
    item = artifact_target(url)
    assert item.url == target
    assert item.filename == name
    assert PlatformDetector.detect(url).media_type == 'document'


@pytest.mark.parametrize('url', [
    'https://github.com/owner/repo/issues/1',
    'https://github.com/owner/repo/releases',
    'http://github.com/owner/repo',
    'https://github.com/owner/repo?token=private',
    'https://github.com/owner/repo/blob/main',
    'https://example.com/file.apk',
])
def test_reject_unsupported_links(url):
    with pytest.raises(InvalidURLError):
        artifact_target(url)


def test_filename_no_subdirectories():
    assert '/' not in safe_filename('folder/name.apk')
    assert safe_filename('') == 'download.bin'


class FakeContent:
    async def iter_chunked(self, _size):
        yield b'hello'
        yield b' world'


@asynccontextmanager
async def fake_response(*args, **kwargs):
    yield SimpleNamespace(content_length=11, content=FakeContent())


async def test_document_download_uses_router(tmp_path, monkeypatch):
    settings = Settings(_env_file=None)
    backend = ArtifactBackend(settings)
    monkeypatch.setattr(backend, '_response', fake_response)
    manager = DownloadManager(ProviderRouter([backend], register_optional=False))
    url = 'https://github.com/owner/repo/releases/download/v1/app.apk'
    result = await manager.probe(url)
    assert result.platform == 'github' and result.metadata['file_size'] == 11
    progress = []
    result = await manager.download(DownloadRequest(url, tmp_path, progress_hook=progress.append))
    assert result.primary_file.read_bytes() == b'hello world'
    assert result.primary_file.name == 'app.apk'
    assert progress[-1]['downloaded_bytes'] == 11
    assert not list(tmp_path.rglob('*.part'))


async def test_cancel_cleans_partial(tmp_path, monkeypatch):
    backend = ArtifactBackend(Settings(_env_file=None))
    monkeypatch.setattr(backend, '_response', fake_response)
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(CancelledError):
        await backend.download(DownloadRequest('https://github.com/owner/repo', tmp_path, cancel_event=cancel))
    assert not list(tmp_path.iterdir())


async def test_stream_limit_and_cleanup(tmp_path, monkeypatch):
    @asynccontextmanager
    async def unknown_length(*args, **kwargs):
        yield SimpleNamespace(content_length=None, content=FakeContent())

    backend = ArtifactBackend(Settings(_env_file=None))
    monkeypatch.setattr(backend, '_response', unknown_length)
    with pytest.raises(RuntimeError, match='File too large'):
        await backend.download(DownloadRequest('https://github.com/owner/repo', tmp_path, max_bytes=10))
    assert not list(tmp_path.iterdir())
