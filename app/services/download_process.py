"""Bounded yt-dlp subprocess: a wedged extractor cannot occupy a worker forever.

Credentials are delivered over stdin, never process arguments or progress output.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any

from app.config import Settings
from app.errors import CancelledError, ErrorCode, MediaError, classify_error
from app.security import redact_secrets


def _settings_payload(settings: Settings) -> dict[str, Any]:
    payload = settings.model_dump(mode="json")
    for name in type(settings).model_fields:
        value = getattr(settings, name)
        if hasattr(value, "get_secret_value"):
            payload[name] = value.get_secret_value()
    return payload


async def run_download_process(
    settings: Settings, operation: str, url: str, *, quality: str = "best",
    output_dir: Path | None = None, known_qualities=None, limit=None,
    progress_hook=None, cancel_event=None,
) -> dict[str, Any] | list:
    if cancel_event and cancel_event.is_set():
        raise CancelledError("Download cancelled")
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "app.services.download_process",
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        start_new_session=os.name == "posix",
    )

    async def exchange():
        payload = {"settings": _settings_payload(settings), "operation": operation, "url": url,
                   "quality": quality, "output_dir": str(output_dir) if output_dir else None,
                   "known_qualities": known_qualities, "limit": limit}
        process.stdin.write(json.dumps(payload).encode())
        await process.stdin.drain()
        process.stdin.close()
        result = None
        while line := await process.stdout.readline():
            event = json.loads(line)
            if event["type"] == "progress":
                if progress_hook:
                    progress_hook(event["data"])
            elif event["type"] == "error":
                error = MediaError(event["message"])
                error.code = ErrorCode(event["code"])
                error.retryable = event["retryable"]
                raise error
            elif event["type"] == "result":
                result = event["data"]
        await process.wait()
        if process.returncode != 0 or result is None:
            raise RuntimeError("Temporary extractor process failure")
        return result

    async def drain_errors():
        # Do not retain unbounded extractor logs, which can contain signed URLs.
        while await process.stderr.read(8192):
            pass

    async def watch_cancel():
        while not (cancel_event and cancel_event.is_set()):
            await asyncio.sleep(0.1)
        raise CancelledError("Download cancelled")

    exchange_task = asyncio.create_task(exchange())
    errors_task = asyncio.create_task(drain_errors())
    cancel_task = asyncio.create_task(watch_cancel())
    try:
        timeout = settings.download_timeout_seconds if operation == "download" else settings.probe_timeout_seconds
        done, _ = await asyncio.wait({exchange_task, cancel_task}, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
        if not done:
            raise TimeoutError(f"{operation} extractor exceeded {timeout}s deadline")
        if cancel_task in done:
            return await cancel_task
        return await exchange_task
    finally:
        # Kill the process group too: yt-dlp may have spawned FFmpeg.
        if os.name == "posix":
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        elif process.returncode is None:
            process.kill()
        await process.wait()
        for task in (exchange_task, errors_task, cancel_task):
            task.cancel()
        await asyncio.gather(exchange_task, errors_task, cancel_task, return_exceptions=True)


def _emit(kind: str, data: Any) -> None:
    print(json.dumps({"type": kind, "data": data}, ensure_ascii=True), flush=True)


def main() -> None:
    from app.services.downloader import _YtDlpEngine

    payload = json.load(sys.stdin)
    settings = Settings(_env_file=None, **payload["settings"])
    logging.disable(logging.CRITICAL)
    engine = _YtDlpEngine(settings)
    try:
        if payload["operation"] == "probe":
            result = asdict(engine.probe(payload["url"]))
        elif payload["operation"] == "playlist":
            result = [asdict(item) for item in engine.expand_playlist(payload["url"], limit=payload["limit"])]
        else:
            def progress(event):
                keys = ("status", "downloaded_bytes", "total_bytes", "total_bytes_estimate", "speed", "eta")
                _emit("progress", {key: event[key] for key in keys if key in event})
            output = engine.download(payload["url"], payload["quality"], Path(payload["output_dir"]),
                                     progress_hook=progress, known_qualities=payload["known_qualities"])
            result = {"output": str(output)}
        _emit("result", result)
    except Exception as exc:
        info = classify_error(exc)
        message = redact_secrets(exc, bot_token=settings.bot_token, database_url=settings.database_url)
        print(json.dumps({"type": "error", "message": message[:1500], "code": info.code.value,
                          "retryable": info.retryable}), flush=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
