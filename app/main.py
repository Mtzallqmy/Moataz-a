from __future__ import annotations

import asyncio
import logging
import shutil
import time
from contextlib import asynccontextmanager, suppress
from pathlib import Path

import uvicorn
from aiogram import Bot
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select

from app.bot import create_dispatcher
from app.bot.client import create_bot
from app.config import get_settings
from app.dashboard import router as dashboard_router
from app.db import DownloadJob, SessionLocal, init_db
from app.operations import mark_stale_workers_offline, readiness_snapshot, reconcile_stale_jobs
from app.queue import enqueue_download, shutdown_queue, start_queue
from app.security import redact_secrets
from app.services.ai_registry import get_ai_provider_registry
from app.version import RELEASE

settings = get_settings()
logger = logging.getLogger("moataz")
dispatcher = create_dispatcher()
bot: Bot | None = None
polling_task: asyncio.Task | None = None
maintenance_task: asyncio.Task | None = None
ai_probe_task: asyncio.Task | None = None


def _redact(value: object) -> str:
    return redact_secrets(
        value,
        bot_token=settings.bot_token,
        database_url=settings.database_url,
        api_token=settings.openai_api_token,
    )


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = _redact(record.msg)
        if isinstance(record.args, tuple):
            record.args = tuple(_redact(arg) for arg in record.args)
        elif isinstance(record.args, dict):
            record.args = {key: _redact(value) for key, value in record.args.items()}
        return True


def configure_logging() -> None:
    root = logging.getLogger()
    if not any(isinstance(item, RedactingFilter) for item in root.filters):
        root.addFilter(RedactingFilter())
    for handler in root.handlers:
        if not any(isinstance(item, RedactingFilter) for item in handler.filters):
            handler.addFilter(RedactingFilter())


async def _run_polling_forever(bot_instance: Bot) -> None:
    while True:
        try:
            try:
                await bot_instance.delete_webhook(drop_pending_updates=False)
            except Exception as exc:
                logger.warning("deleteWebhook failed with %s; polling will still be attempted", type(exc).__name__)
            await dispatcher.start_polling(
                bot_instance,
                allowed_updates=dispatcher.resolve_used_update_types(),
                handle_signals=False,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("Telegram polling failed with %s; retrying", type(exc).__name__)
            await asyncio.sleep(5)


async def _cleanup_temp_dirs() -> None:
    try:
        async with SessionLocal() as session:
            active = set(
                await session.scalars(
                    select(DownloadJob.id).where(
                        DownloadJob.status.in_(["QUEUED", "RETRYING", "DOWNLOADING", "MERGING", "PROCESSING", "CUTTING", "UPLOADING"])
                    )
                )
            )
        root = settings.download_dir
        if not root.exists():
            return
        cutoff = time.time() - settings.temp_retention_seconds
        for path in root.iterdir():
            if not path.is_dir() or not path.name.isdigit() or int(path.name) in active:
                continue
            if path.stat().st_mtime < cutoff:
                shutil.rmtree(path, ignore_errors=True)
    except Exception as exc:
        logger.warning("Temp cleanup failed with %s", type(exc).__name__)


async def _maintenance_loop() -> None:
    while True:
        try:
            await mark_stale_workers_offline()
            await _cleanup_temp_dirs()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("Maintenance failed with %s", type(exc).__name__)
        await asyncio.sleep(60)


async def _probe_ai_providers() -> None:
    await asyncio.sleep(2)
    registry = get_ai_provider_registry()
    if not registry.enabled:
        logger.info("AI provider registry is disabled")
        return
    try:
        checks = await registry.probe()
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.warning("AI provider probe failed with %s", type(exc).__name__)
        return
    for check in checks:
        name = str(check.get("provider_name") or check.get("provider_id") or "AI")
        if check.get("ok") is True:
            capabilities = ",".join(str(item) for item in check.get("capabilities") or []) or "text"
            logger.info(
                "AI provider %s ready: %s models; capabilities=%s",
                name,
                int(check.get("model_count") or 0),
                capabilities,
            )
        else:
            logger.warning("AI provider %s unavailable: %s", name, str(check.get("error") or "unknown error")[:180])


@asynccontextmanager
async def lifespan(app: FastAPI):  # noqa: ARG001
    global bot, polling_task, maintenance_task, ai_probe_task
    configure_logging()
    if not settings.bot_token.strip():
        raise RuntimeError("BOT_TOKEN is required")
    await init_db()
    await start_queue()
    reconciliation = await reconcile_stale_jobs()
    for job_id in reconciliation.requeue_ids:
        await enqueue_download(job_id)
    await mark_stale_workers_offline()

    bot = create_bot()
    polling_task = asyncio.create_task(_run_polling_forever(bot), name="telegram-polling")
    maintenance_task = asyncio.create_task(_maintenance_loop(), name="maintenance")
    ai_probe_task = asyncio.create_task(_probe_ai_providers(), name="ai-provider-probe")
    logger.info("Starting %s release %s", settings.app_name, RELEASE)
    try:
        yield
    finally:
        if polling_task:
            polling_task.cancel()
            with suppress(asyncio.CancelledError):
                await polling_task
        if maintenance_task:
            maintenance_task.cancel()
            with suppress(asyncio.CancelledError):
                await maintenance_task
        if ai_probe_task:
            ai_probe_task.cancel()
            with suppress(asyncio.CancelledError):
                await ai_probe_task
        await shutdown_queue()
        if bot:
            await bot.session.close()


app = FastAPI(title=settings.app_name, lifespan=lifespan)
static_dir = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")
app.include_router(dashboard_router)


@app.get("/")
async def root():
    return {
        "name": settings.app_name,
        "status": "ok",
        "release": RELEASE,
        "mode": "polling",
        "queue": "inline",
        "dashboard": "/dashboard" if settings.dashboard_password else "disabled",
        "ai_provider": "configured" if get_ai_provider_registry().enabled else "disabled",
    }


@app.get("/healthz")
async def healthz():
    return {"status": "ok", "release": RELEASE}


@app.get("/readyz")
async def readyz():
    checks = await readiness_snapshot()
    return JSONResponse(
        status_code=200 if checks["ok"] else 503,
        content={"status": "ready" if checks["ok"] else "not_ready", "release": RELEASE, **checks},
    )


@app.get("/version")
async def version():
    return {"release": RELEASE}


if __name__ == "__main__":
    # Pass the ASGI app object directly: app.main is never imported a second time.
    uvicorn.run(app, host=settings.app_host, port=settings.app_port, reload=False)
