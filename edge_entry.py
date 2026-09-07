"""Moataz Edge managed runtime adapter.

This module lets the Android host own Telegram polling and the process lifecycle while
reusing the production aiogram handlers, SQLite database and background download queue.
It deliberately does not start FastAPI, Uvicorn or another Telegram polling loop.
"""

from __future__ import annotations

import json
import os
from contextlib import suppress
from typing import Any

from aiogram.types import Update

from app.bot import create_dispatcher
from app.bot.client import create_bot
from app.db import init_db
from app.operations import mark_stale_workers_offline, reconcile_stale_jobs
from app.queue import enqueue_download, shutdown_queue, start_queue

_dispatcher = None
_bot = None
_started = False
_runtime: dict[str, Any] = {}


async def startup(runtime_config: dict[str, Any] | None = None) -> None:
    """Initialize persistent application state once per hosted repository revision."""
    global _dispatcher, _bot, _started, _runtime
    if _started:
        return

    _runtime = dict(runtime_config or {})
    if not os.environ.get("BOT_TOKEN", "").strip():
        raise RuntimeError("BOT_TOKEN was not provided by Moataz Edge")

    await init_db()
    await start_queue()

    reconciliation = await reconcile_stale_jobs()
    for job_id in reconciliation.requeue_ids:
        await enqueue_download(job_id)
    await mark_stale_workers_offline()

    _dispatcher = create_dispatcher()
    _bot = create_bot()
    _started = True


async def process_update(raw_update: str, config_json: str) -> dict[str, Any]:
    """Feed one host-received Telegram update into the existing aiogram Dispatcher."""
    if not _started:
        await startup({})
    if _dispatcher is None or _bot is None:
        raise RuntimeError("Hosted Telegram service is not initialized")

    try:
        update = Update.model_validate_json(raw_update)
    except Exception as exc:
        raise RuntimeError(f"Invalid Telegram update: {exc}") from exc

    # The Android host owns polling. aiogram still owns routing, FSM, keyboards,
    # Telegram API actions, DB writes and the background media queue.
    await _dispatcher.feed_update(_bot, update)
    return {
        "action": "drop",
        "output_text": None,
        "note": "aiogram update handled by Moataz-a hosted service",
    }


async def health() -> dict[str, Any]:
    return {
        "ok": bool(_started and _dispatcher is not None and _bot is not None),
        "detail": "aiogram dispatcher + SQLite queue ready" if _started else "service not started",
        "mode": "telegram-service",
        "data_dir": os.environ.get("EDGE_DATA_DIR", ""),
    }


async def shutdown() -> None:
    global _dispatcher, _bot, _started
    if not _started:
        return
    with suppress(Exception):
        await shutdown_queue()
    if _bot is not None:
        with suppress(Exception):
            await _bot.session.close()
    _dispatcher = None
    _bot = None
    _started = False
