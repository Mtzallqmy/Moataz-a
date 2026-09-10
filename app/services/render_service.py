from __future__ import annotations

import asyncio
import logging
import shutil
import threading
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select

from app.config import Settings, get_settings
from app.db import MediaProject, ProjectStatus, RenderJob, RenderStatus, SessionLocal
from app.errors import CancelledError
from app.security import redact_secrets
from app.services.composer import ComposerService, composer_service
from app.services.media import probe_media_file
from app.services.renderers.base import BaseRenderer
from app.services.renderers.ffmpeg_renderer import ffmpeg_renderer

logger = logging.getLogger("moataz.studio.render")
_ACTIVE_STATES = {
    RenderStatus.PREPARING.value,
    RenderStatus.RENDERING.value,
    RenderStatus.UPLOADING.value,
}


class RenderService:
    def __init__(
        self,
        settings: Settings | None = None,
        *,
        composer: ComposerService | None = None,
        renderer: BaseRenderer | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.composer = composer or composer_service
        self.renderer = renderer or ffmpeg_renderer
        self._cancel_events: dict[int, threading.Event] = {}
        self._lock = threading.Lock()
        self._progress_cache: dict[int, float] = {}

    def _event(self, render_job_id: int) -> threading.Event:
        with self._lock:
            return self._cancel_events.setdefault(int(render_job_id), threading.Event())

    def _release(self, render_job_id: int) -> None:
        with self._lock:
            self._cancel_events.pop(int(render_job_id), None)
        self._progress_cache.pop(int(render_job_id), None)

    def request_cancel(self, render_job_id: int) -> bool:
        event = self._event(render_job_id)
        already = event.is_set()
        event.set()
        return not already

    async def create_render(self, project_id: int, *, user_id: int) -> RenderJob:
        await self.composer.build(project_id, user_id=user_id)
        async with SessionLocal() as session:
            project = await session.scalar(
                select(MediaProject).where(MediaProject.id == project_id, MediaProject.user_id == user_id)
            )
            if project is None:
                raise LookupError("Project not found")
            if project.status == ProjectStatus.CANCELLED.value:
                raise ValueError("Cancelled project cannot be rendered")
            active = await session.scalar(
                select(RenderJob.id).where(
                    RenderJob.project_id == project_id,
                    RenderJob.status.in_(
                        [
                            RenderStatus.QUEUED.value,
                            RenderStatus.PREPARING.value,
                            RenderStatus.RENDERING.value,
                            RenderStatus.UPLOADING.value,
                        ]
                    ),
                ).limit(1)
            )
            if active is not None:
                raise ValueError("Project already has an active render")
            job = RenderJob(
                project_id=project.id,
                user_id=user_id,
                status=RenderStatus.QUEUED.value,
                progress=0.0,
            )
            session.add(job)
            project.status = ProjectStatus.READY.value
            await session.commit()
            await session.refresh(job)
            session.expunge(job)
            return job

    async def get_render(self, render_job_id: int, *, user_id: int | None = None) -> RenderJob | None:
        async with SessionLocal() as session:
            statement = select(RenderJob).where(RenderJob.id == render_job_id)
            if user_id is not None:
                statement = statement.where(RenderJob.user_id == user_id)
            job = await session.scalar(statement)
            if job is not None:
                session.expunge(job)
            return job

    async def get_render_user_id(self, render_job_id: int) -> int:
        async with SessionLocal() as session:
            user_id = await session.scalar(select(RenderJob.user_id).where(RenderJob.id == render_job_id))
        if user_id is None:
            raise LookupError("Render job not found")
        return int(user_id)

    async def _set_progress(self, render_job_id: int, value: float) -> None:
        value = max(0.0, min(float(value), 0.99))
        previous = self._progress_cache.get(render_job_id, -1.0)
        if previous >= 0 and value < 0.99 and value - previous < 0.01:
            return
        self._progress_cache[render_job_id] = value
        async with SessionLocal() as session:
            job = await session.get(RenderJob, render_job_id)
            if job is not None and job.status == RenderStatus.RENDERING.value:
                job.progress = max(float(job.progress or 0), value)
                await session.commit()

    async def process_render(self, render_job_id: int) -> None:
        event = self._event(render_job_id)
        output_path: Path | None = None
        try:
            async with SessionLocal() as session:
                job = await session.get(RenderJob, render_job_id)
                if job is None:
                    raise LookupError("Render job not found")
                if job.status == RenderStatus.CANCELLED.value:
                    return
                if job.status not in {RenderStatus.QUEUED.value, RenderStatus.PREPARING.value}:
                    raise ValueError(f"Render job is not runnable from status {job.status}")
                project = await session.get(MediaProject, job.project_id)
                if project is None:
                    raise LookupError("Project not found")
                job.status = RenderStatus.PREPARING.value
                job.started_at = datetime.now(UTC)
                job.error = None
                project.status = ProjectStatus.RENDERING.value
                project_id = project.id
                user_id = job.user_id
                await session.commit()

            if event.is_set():
                raise CancelledError("Render cancelled before preparation completed")
            plan = await self.composer.build(project_id, user_id=user_id)
            async with SessionLocal() as session:
                job = await session.get(RenderJob, render_job_id)
                if job is None:
                    raise LookupError("Render job disappeared")
                job.status = RenderStatus.RENDERING.value
                job.progress = max(float(job.progress or 0), 0.01)
                await session.commit()

            result = await asyncio.wait_for(
                self.renderer.render(
                    plan,
                    render_job_id=render_job_id,
                    progress_callback=lambda value: self._set_progress(render_job_id, value),
                    cancel_event=event,
                ),
                timeout=self.settings.render_timeout_seconds,
            )
            output_path = result.output_path
            if event.is_set():
                raise CancelledError("Render cancelled")
            expected_output_dir = (
                self.settings.project_dir / str(project_id) / "renders" / str(render_job_id)
            ).resolve()
            resolved_output = result.output_path.resolve()
            if expected_output_dir not in resolved_output.parents:
                raise ValueError("Renderer returned an unsafe output path")
            if not result.output_path.exists() or result.output_path.stat().st_size <= 0:
                raise ValueError("Renderer returned a missing or empty output")
            async with SessionLocal() as session:
                job = await session.get(RenderJob, render_job_id)
                project = await session.get(MediaProject, project_id)
                if job is None or project is None:
                    raise LookupError("Render database state disappeared")
                if project.status == ProjectStatus.CANCELLED.value or event.is_set():
                    raise CancelledError("Project or render was cancelled")
                job.status = RenderStatus.COMPLETED.value
                job.progress = 1.0
                job.output_path = str(result.output_path)
                job.file_size = int(result.file_size)
                job.error = None
                job.completed_at = datetime.now(UTC)
                project.status = ProjectStatus.COMPLETED.value
                await session.commit()
            logger.info(
                "render completed project_id=%s render_job_id=%s operation=render duration=%.3f ffmpeg_exit_status=0",
                project_id,
                render_job_id,
                result.duration,
            )
        except asyncio.CancelledError:
            if output_path is not None:
                output_path.unlink(missing_ok=True)
            await self._finish_failed(render_job_id, cancelled=True, message="Render cancelled")
            raise
        except CancelledError:
            if output_path is not None:
                output_path.unlink(missing_ok=True)
            await self._finish_failed(render_job_id, cancelled=True, message="Render cancelled")
        except TimeoutError:
            if output_path is not None:
                output_path.unlink(missing_ok=True)
            await self._finish_failed(render_job_id, cancelled=False, message="Render timed out")
        except Exception as exc:
            if output_path is not None:
                output_path.unlink(missing_ok=True)
            safe = redact_secrets(
                exc,
                bot_token=self.settings.bot_token,
                database_url=self.settings.database_url,
                api_token=self.settings.openai_api_token,
            )[-1000:]
            await self._finish_failed(render_job_id, cancelled=False, message=safe)
            logger.warning(
                "render failed render_job_id=%s operation=render error=%s",
                render_job_id,
                type(exc).__name__,
            )
        finally:
            self._release(render_job_id)

    async def _finish_failed(self, render_job_id: int, *, cancelled: bool, message: str) -> None:
        async with SessionLocal() as session:
            job = await session.get(RenderJob, render_job_id)
            if job is None:
                return
            project = await session.get(MediaProject, job.project_id)
            job.status = RenderStatus.CANCELLED.value if cancelled else RenderStatus.FAILED.value
            job.error = None if cancelled else message[:1000]
            job.completed_at = datetime.now(UTC)
            if project is not None and project.status != ProjectStatus.CANCELLED.value:
                project.status = ProjectStatus.READY.value
            await session.commit()

    async def cancel_render(self, render_job_id: int, *, user_id: int | None = None) -> bool:
        async with SessionLocal() as session:
            statement = select(RenderJob).where(RenderJob.id == render_job_id)
            if user_id is not None:
                statement = statement.where(RenderJob.user_id == user_id)
            job = await session.scalar(statement)
            if job is None:
                return False
            if job.status in {
                RenderStatus.COMPLETED.value,
                RenderStatus.FAILED.value,
                RenderStatus.CANCELLED.value,
            }:
                return False
            self.request_cancel(render_job_id)
            if job.status in {RenderStatus.QUEUED.value, RenderStatus.UPLOADING.value}:
                job.status = RenderStatus.CANCELLED.value
                job.completed_at = datetime.now(UTC)
                project = await session.get(MediaProject, job.project_id)
                if project is not None and project.status != ProjectStatus.CANCELLED.value:
                    project.status = ProjectStatus.READY.value
                await session.commit()
            return True

    async def cancel_project_renders(self, project_id: int, *, user_id: int) -> int:
        async with SessionLocal() as session:
            render_ids = list(
                await session.scalars(
                    select(RenderJob.id).where(
                        RenderJob.project_id == project_id,
                        RenderJob.user_id == user_id,
                        RenderJob.status.in_(
                            {
                                RenderStatus.QUEUED.value,
                                RenderStatus.PREPARING.value,
                                RenderStatus.RENDERING.value,
                                RenderStatus.UPLOADING.value,
                            }
                        ),
                    )
                )
            )
        cancelled = 0
        for render_id in render_ids:
            cancelled += bool(await self.cancel_render(render_id, user_id=user_id))
        return cancelled

    async def mark_uploading(self, render_job_id: int) -> None:
        async with SessionLocal() as session:
            job = await session.get(RenderJob, render_job_id)
            if (
                job is not None
                and job.status == RenderStatus.COMPLETED.value
                and job.output_path
                and Path(job.output_path).is_file()
            ):
                job.status = RenderStatus.UPLOADING.value
                await session.commit()

    async def finish_delivery(self, render_job_id: int, *, error: str | None = None) -> None:
        async with SessionLocal() as session:
            job = await session.get(RenderJob, render_job_id)
            if job is None:
                return
            if job.output_path and Path(job.output_path).exists():
                job.status = RenderStatus.COMPLETED.value
                job.error = f"DELIVERY_FAILED: {error[:800]}" if error else None
                await session.commit()

    async def reconcile_after_restart(self) -> list[int]:
        requeue: list[int] = []
        async with SessionLocal() as session:
            queued = list(
                await session.scalars(select(RenderJob).where(RenderJob.status == RenderStatus.QUEUED.value))
            )
            requeue.extend(job.id for job in queued)
            interrupted = list(
                await session.scalars(
                    select(RenderJob).where(
                        RenderJob.status.in_(
                            {RenderStatus.PREPARING.value, RenderStatus.RENDERING.value}
                        )
                    )
                )
            )
            for job in interrupted:
                job.status = RenderStatus.FAILED.value
                job.error = "RESTART_INTERRUPTED: render can be retried safely"
                job.completed_at = datetime.now(UTC)
                project = await session.get(MediaProject, job.project_id)
                if project is not None and project.status != ProjectStatus.CANCELLED.value:
                    project.status = ProjectStatus.READY.value
            uploading = list(
                await session.scalars(
                    select(RenderJob).where(RenderJob.status == RenderStatus.UPLOADING.value)
                )
            )
            for job in uploading:
                valid_output = False
                if job.output_path:
                    try:
                        output = Path(job.output_path)
                        expected = (
                            self.settings.project_dir / str(job.project_id) / "renders" / str(job.id)
                        ).resolve()
                        resolved = output.resolve()
                        if expected in resolved.parents and output.is_file() and output.stat().st_size > 0:
                            probe = await probe_media_file(output)
                            valid_output = probe.has_video and probe.duration > 0
                    except Exception:
                        valid_output = False
                if valid_output:
                    job.status = RenderStatus.COMPLETED.value
                    job.error = "DELIVERY_INTERRUPTED: output is ready; delivery can be retried"
                else:
                    job.status = RenderStatus.FAILED.value
                    job.error = "RESTART_INTERRUPTED: upload output is missing or invalid"
                    project = await session.get(MediaProject, job.project_id)
                    if project is not None and project.status != ProjectStatus.CANCELLED.value:
                        project.status = ProjectStatus.READY.value
                job.completed_at = datetime.now(UTC)
            await session.commit()
        self.settings.render_temp_dir.mkdir(parents=True, exist_ok=True)
        for workspace in self.settings.render_temp_dir.glob("render-*"):
            if not workspace.is_dir():
                continue
            suffix = workspace.name.removeprefix("render-")
            if suffix.isdigit():
                shutil.rmtree(workspace, ignore_errors=True)
        return requeue


render_service = RenderService()
