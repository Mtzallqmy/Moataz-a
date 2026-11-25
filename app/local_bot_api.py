"""Optional official local Telegram Bot API supervisor for the Railway service.

Cloud mode execs the unchanged app. Local mode requires explicit configuration,
valid application credentials and a compiled upstream telegram-bot-api binary.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from app.config import Settings, get_settings


class LocalAPIError(RuntimeError):
    """A safe-to-log startup error; never include credentials or request URLs."""


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _bot_request(base: str, bot_token: str, method: str, *, timeout: float = 10) -> object:
    if base != "https://api.telegram.org" and not re.fullmatch(r"http://127\.0\.0\.1:\d+", base):
        raise LocalAPIError("Invalid Telegram API endpoint")
    if not re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]+", bot_token):
        raise LocalAPIError("BOT_TOKEN is missing or invalid")
    if method not in {"logOut", "getMe"}:
        raise LocalAPIError("Unsupported initialization operation")
    opener = build_opener(ProxyHandler({}), _NoRedirect())
    request = Request(f"{base}/bot{bot_token}/{method}", data=b"", method="POST")
    try:
        with opener.open(request, timeout=timeout) as response:
            payload = json.loads(response.read(65536))
        if isinstance(payload, dict) and payload.get("ok"):
            return payload.get("result")
    except (HTTPError, URLError, OSError, ValueError):
        pass
    raise LocalAPIError(f"Telegram initialization operation {method} failed")


def _credentials() -> tuple[str, str]:
    api_id = os.environ.get("TELEGRAM_API_ID", "").strip()
    api_hash = os.environ.get("TELEGRAM_API_HASH", "").strip()
    if not re.fullmatch(r"[1-9][0-9]{0,9}", api_id) or not re.fullmatch(r"[a-fA-F0-9]{32}", api_hash):
        raise LocalAPIError("Local API requires TELEGRAM_API_ID and TELEGRAM_API_HASH from my.telegram.org/apps")
    return api_id, api_hash


def _server_command(binary: str, settings: Settings, state_dir: Path, temp_dir: Path) -> list[str]:
    return [binary, "--local", "--http-ip-address=127.0.0.1", f"--http-port={settings.telegram_local_api_port}", f"--dir={state_dir}", f"--temp-dir={temp_dir}", "--verbosity=0"]


def _wait_for_listener(process: subprocess.Popen, port: int, timeout: float = 30) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise LocalAPIError("Local Bot API server exited during startup")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                return
        except OSError:
            time.sleep(0.5)
    raise LocalAPIError("Local Bot API server did not open its loopback port")


def _migration_marker(state_dir: Path, token: str) -> Path:
    digest = hashlib.sha256(token.encode()).hexdigest()[:24]
    return state_dir / f".cloud-logout-{digest}.done"


def _migrate_once(settings: Settings, state_dir: Path) -> None:
    marker = _migration_marker(state_dir, settings.bot_token)
    if marker.is_file():
        return
    # Telegram documents that cloud polling must be logged out before local login.
    # This only executes after explicit local enablement and a running listener.
    if _bot_request("https://api.telegram.org", settings.bot_token, "logOut") is not True:
        raise LocalAPIError("Cloud Bot API logout was not confirmed")
    with marker.open("x", encoding="utf-8") as stream:
        stream.write("Migration confirmed by Telegram\n")
    marker.chmod(0o600)


def _wait_for_bot(settings: Settings, process: subprocess.Popen, timeout: float = 45) -> None:
    deadline = time.monotonic() + timeout
    base = f"http://127.0.0.1:{settings.telegram_local_api_port}"
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise LocalAPIError("Local Bot API server exited before bot authentication")
        try:
            result = _bot_request(base, settings.bot_token, "getMe", timeout=5)
            if isinstance(result, dict) and result.get("is_bot"):
                return
        except LocalAPIError:
            time.sleep(2)
    raise LocalAPIError("Local Bot API connection could not authenticate the bot")


def _stop(process: subprocess.Popen | None, *, timeout: float = 20) -> None:
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def run_local(settings: Settings) -> int:
    api_id, api_hash = _credentials()
    binary = shutil.which("telegram-bot-api")
    if not binary:
        raise LocalAPIError("Local API binary is missing; build with Dockerfile.telegram-local before enabling")
    state_dir = settings.download_dir.parent / "telegram-bot-api"
    temp_dir = Path("/tmp/telegram-bot-api")
    state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    temp_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    env = {key: os.environ[key] for key in ("PATH", "LANG", "LC_ALL", "SSL_CERT_FILE", "SSL_CERT_DIR", "TZ") if key in os.environ}
    env.update(TELEGRAM_API_ID=api_id, TELEGRAM_API_HASH=api_hash)
    server = None
    app = None
    stopping = False

    def request_stop(_sig, _frame):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    try:
        server = subprocess.Popen(_server_command(binary, settings, state_dir, temp_dir), env=env)
        _wait_for_listener(server, settings.telegram_local_api_port)
        if stopping:
            return 0
        _migrate_once(settings, state_dir)
        _wait_for_bot(settings, server)
        if stopping:
            return 0
        print("Official local Telegram Bot API ready; original artifact uploads enabled", flush=True)
        app = subprocess.Popen([sys.executable, "-m", "app.main"])
        while not stopping:
            if app.poll() is not None:
                return app.returncode or 0
            if server.poll() is not None:
                print("Local Telegram API exited; restarting service", file=sys.stderr, flush=True)
                return 1
            time.sleep(0.5)
        return 0
    finally:
        # Let the web application stop its polling and durable job workers first.
        _stop(app)
        _stop(server)


def main() -> int:
    settings = get_settings()
    if not settings.telegram_local_api_enabled:
        os.execv(sys.executable, [sys.executable, "-m", "app.main"])
    try:
        return run_local(settings)
    except (LocalAPIError, OSError) as exc:
        print(str(exc) if isinstance(exc, LocalAPIError) else "Local Bot API startup failed", file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
