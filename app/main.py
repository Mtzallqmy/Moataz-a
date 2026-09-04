from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, suppress

import uvicorn
from aiogram import Bot
from aiogram.types import Update
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.staticfiles import StaticFiles

from app.bot import create_dispatcher
from app.bot.client import create_bot
from app.config import get_settings
from app.dashboard import router as dashboard_router
from app.db import init_db

settings = get_settings()
dispatcher = create_dispatcher()
bot: Bot | None = None
polling_task: asyncio.Task | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):  # noqa: ARG001
    global bot, polling_task
    if not settings.bot_token.strip():
        raise RuntimeError("BOT_TOKEN is required")

    await init_db()
    bot = create_bot()

    if settings.app_mode == "webhook":
        if not settings.webhook_base_url:
            raise RuntimeError("WEBHOOK_BASE_URL is required only when APP_MODE=webhook")
        webhook_kwargs = {
            "allowed_updates": dispatcher.resolve_used_update_types(),
            "drop_pending_updates": False,
        }
        if settings.webhook_secret:
            webhook_kwargs["secret_token"] = settings.webhook_secret
        await bot.set_webhook(settings.webhook_url, **webhook_kwargs)
    else:
        await bot.delete_webhook(drop_pending_updates=False)
        polling_task = asyncio.create_task(
            dispatcher.start_polling(bot, allowed_updates=dispatcher.resolve_used_update_types())
        )

    try:
        yield
    finally:
        if polling_task:
            polling_task.cancel()
            with suppress(asyncio.CancelledError):
                await polling_task
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
        "dashboard": "/dashboard",
        "mode": settings.app_mode,
        "queue": settings.queue_backend,
    }


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}


@app.post("/telegram/webhook")
async def telegram_webhook(
    request: Request,
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
):
    if settings.app_mode != "webhook":
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
    # APP_PORT is supported locally; Railway injects PORT and config.py maps it automatically.
    uvicorn.run("app.main:app", host=settings.app_host, port=settings.app_port, reload=False)
