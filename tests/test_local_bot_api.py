from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from app import local_bot_api
from app.bot import client
from app.config import Settings

MIB = 1024 * 1024
BOT_TOKEN = '123456:ABCDEFGHIJKLMNOPQRSTUVWXYZ1234567890'


def test_mode_selects_200_mb_only_with_local_bot_api():
    cloud = Settings(_env_file=None, artifact_download_limit_mb=200, telegram_upload_limit_mb=200)
    local = Settings(_env_file=None, artifact_download_limit_mb=200, telegram_local_api_enabled=True)
    assert cloud.artifact_telegram_upload_limit_bytes == 49*MIB
    assert local.artifact_telegram_upload_limit_bytes == 200*MIB
    assert local.telegram_upload_limit_bytes == 49*MIB  # existing media behavior


@pytest.mark.parametrize('enabled', [False, True])
async def test_aiogram_session_switches_only_when_enabled(monkeypatch, enabled):
    settings = Settings(_env_file=None, bot_token=BOT_TOKEN, telegram_local_api_enabled=enabled)
    monkeypatch.setattr(client, 'get_settings', lambda: settings)
    bot = client.create_bot()
    try:
        assert bot.session.api.is_local == enabled
        assert ('127.0.0.1:8081' in bot.session.api.base) == enabled
    finally:
        await bot.session.close()


def test_local_api_requires_own_application_credentials(monkeypatch):
    monkeypatch.delenv('TELEGRAM_API_ID', raising=False)
    monkeypatch.delenv('TELEGRAM_API_HASH', raising=False)
    with pytest.raises(local_bot_api.LocalAPIError, match='TELEGRAM_API_ID'):
        local_bot_api._credentials()


def test_migration_only_after_confirmed_logout_once(tmp_path, monkeypatch):
    settings = Settings(_env_file=None, bot_token=BOT_TOKEN, telegram_local_api_enabled=True)
    calls = []

    def fake_request(base, token, method, **kwargs):
        calls.append((base, token, method))
        return True

    monkeypatch.setattr(local_bot_api, '_bot_request', fake_request)
    local_bot_api._migrate_once(settings, tmp_path)
    local_bot_api._migrate_once(settings, tmp_path)
    assert calls == [('https://api.telegram.org', BOT_TOKEN, 'logOut')]
    assert local_bot_api._migration_marker(tmp_path, BOT_TOKEN).is_file()


def test_failed_logout_does_not_create_marker(tmp_path, monkeypatch):
    settings = Settings(_env_file=None, bot_token=BOT_TOKEN, telegram_local_api_enabled=True)
    monkeypatch.setattr(local_bot_api, '_bot_request', lambda *args, **kwargs: False)
    with pytest.raises(local_bot_api.LocalAPIError):
        local_bot_api._migrate_once(settings, tmp_path)
    assert not local_bot_api._migration_marker(tmp_path, BOT_TOKEN).exists()


def test_server_is_bound_to_loopback_without_credentials_in_arguments(tmp_path):
    settings = Settings(_env_file=None, telegram_local_api_enabled=True)
    command = local_bot_api._server_command('telegram-bot-api', settings, tmp_path, tmp_path/'temp')
    assert '--local' in command and '--http-ip-address=127.0.0.1' in command
    assert all('api-hash' not in arg and 'api-id' not in arg for arg in command)


async def test_artifact_upload_sends_original_local_file_uri(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from app import worker

    file = tmp_path / 'sample.apk'
    file.write_bytes(b'demo')
    bot = SimpleNamespace(send_document=AsyncMock(), session=SimpleNamespace(close=AsyncMock()))
    monkeypatch.setattr(worker.settings, 'telegram_local_api_enabled', True)
    monkeypatch.setattr(worker, 'create_bot', lambda: bot)
    monkeypatch.setattr(worker, '_job_delivery', AsyncMock(return_value=(10, None)))

    class Session:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *_):
            pass
        async def scalar(self, _):
            return 'github'

    monkeypatch.setattr(worker, 'SessionLocal', Session)
    await worker._upload_once(1, file)
    assert bot.send_document.await_args.kwargs['document'] == file.as_uri()
    assert bot.send_document.await_args.kwargs['chat_id'] == 10


def test_cloud_entrypoint_preserves_previous_service_without_credentials(monkeypatch):
    settings = Settings(_env_file=None, telegram_local_api_enabled=False)
    monkeypatch.setattr(local_bot_api, 'get_settings', lambda: settings)
    calls = []

    def fake_exec(path, argv):
        calls.append((path, argv))
        raise SystemExit(0)

    monkeypatch.setattr(local_bot_api.os, 'execv', fake_exec)
    with pytest.raises(SystemExit) as exc:
        local_bot_api.main()
    assert exc.value.code == 0 and calls[0][1][-2:] == ['-m', 'app.main']
