from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager, suppress

import uvicorn
from aiogram import Bot
from aiogram.types import Update
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from app.bot import create_dispatcher
from app.bot.client import create_bot
from app.config import get_settings
from app.dashboard import router as dashboard_router
from app.db import init_db
from app.operations import (
    fail_interrupted_inline_jobs,
    mark_stale_workers_offline,
    readiness_snapshot,
)

RELEASE = "0.3.0-phase456"
logger = logging.getLogger(__name__)
settings = get_settings()
dispatcher = create_dispatcher()
bot: Bot | None = None
polling_task: asyncio.Task | None = None
maintenance_task: asyncio.Task | None = None
active_mode = "polling"


async def _run_polling_forever(bot_instance: Bot) -> None:
    """Keep polling alive without taking down the web process on Telegram API failures."""
    while True:
        try:
            await bot_instance.delete_webhook(drop_pending_updates=False)
            await dispatcher.start_polling(
                bot_instance,
                allowed_updates=dispatcher.resolve_used_update_types(),
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Never log the request URL here because Telegram URLs contain the bot token.
            logger.warning(
                "Telegram polling failed with %s; retrying in 5 seconds",
                type(exc).__name__,
            )
            await asyncio.sleep(5)


async def _maintenance_loop() -> None:
    while True:
        try:
            await mark_stale_workers_offline()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("Maintenance pass failed with %s", type(exc).__name__)
        await asyncio.sleep(30)


@asynccontextmanager
async def lifespan(app: FastAPI):  # noqa: ARG001
    global bot, polling_task, maintenance_task, active_mode
    if not settings.bot_token.strip():
        raise RuntimeError("BOT_TOKEN is required")

    await init_db()
    if settings.queue_backend == "inline":
        interrupted = await fail_interrupted_inline_jobs()
        if interrupted:
            logger.warning("Closed %s interrupted inline jobs after restart", len(interrupted))
    await mark_stale_workers_offline()
    maintenance_task = asyncio.create_task(_maintenance_loop())

    bot = create_bot()
    logger.info("Starting Moataz Media Bot release %s", RELEASE)

    use_webhook = settings.app_mode == "webhook" and bool(settings.webhook_base_url.strip())
    if use_webhook:
        webhook_kwargs = {
            "allowed_updates": dispatcher.resolve_used_update_types(),
            "drop_pending_updates": False,
        }
        if settings.webhook_secret:
            webhook_kwargs["secret_token"] = settings.webhook_secret
        try:
            await bot.set_webhook(settings.webhook_url, **webhook_kwargs)
            active_mode = "webhook"
        except Exception as exc:
            # An optional webhook setting must never make Railway crash.
            logger.warning(
                "Webhook setup failed with %s; falling back to polling",
                type(exc).__name__,
            )
            active_mode = "polling"
            polling_task = asyncio.create_task(_run_polling_forever(bot))
    else:
        active_mode = "polling"
        polling_task = asyncio.create_task(_run_polling_forever(bot))

    try:
        yield
    finally:
        for task in (polling_task, maintenance_task):
            if task:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
        if bot:
            await bot.session.close()


app = FastAPI(title=settings.app_name, lifespan=lifespan)
app.mount("/static", StaticFiles(directory="app/static"), name="static")
app.include_router(dashboard_router)


@app.get("/")
async def root():
    return {
        "name": settings.app_name,
        "status": "ok",
        "release": RELEASE,
        "dashboard": "/dashboard",
        "mode": active_mode,
        "queue": settings.queue_backend,
        "adaptive_upload": settings.auto_compress_enabled,
    }


@app.get("/healthz")
async def healthz():
    return {"status": "ok", "release": RELEASE}


@app.get("/readyz")
async def readyz():
    checks = await readiness_snapshot()
    status_code = 200 if checks["ok"] else 503
    return JSONResponse(
        status_code=status_code,
        content={"status": "ready" if checks["ok"] else "not_ready", "release": RELEASE, **checks},
    )


@app.post("/telegram/webhook")
async def telegram_webhook(
    request: Request,
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
):
    if active_mode != "webhook":
        raise HTTPException(status_code=404, detail="Webhook mode disabled")
    if settings.webhook_secret and x_telegram_bot_api_secret_token != settings.webhook_secret:
        raise HTTPException(status_code=401, detail="Invalid webhook secret")
    if bot is None:
        raise HTTPException(status_code=503, detail="Bot is not ready")
    payload = await request.json()
    update = Update.model_validate(payload, context={"bot": bot})
    await dispatcher.feed_update(bot, update)
    return {"ok": True}


if __name__ == "__main__":
    # Pass the ASGI app object directly to avoid importing app.main twice.
    uvicorn.run(app, host=settings.app_host, port=settings.app_port, reload=False)
