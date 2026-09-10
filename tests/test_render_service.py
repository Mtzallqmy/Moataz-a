from __future__ import annotations

import asyncio
import itertools
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from app.config import Settings
from app.db import MediaProject, ProjectStatus, RenderJob, RenderStatus, SessionLocal, User, init_db
from app.errors import CancelledError, FFmpegError
from app.render_queue import RenderQueue
from app.services.composer import RenderPlan
from app.services.projects import ProjectService
from app.services.render_service import RenderService
from app.services.renderers.base import BaseRenderer, RenderResult, emit_progress

_IDS = itertools.count(993000)


class StaticComposer:
    async def build(self, project_id: int, *, user_id: int | None = None) -> RenderPlan:
        return RenderPlan(
            project_id=project_id,
            user_id=int(user_id or 0),
            template="audio_image",
            width=360,
            height=640,
            fps=24,
            aspect_ratio="9:16",
            fit_mode="fit",
            transition="none",
            audio_mode="replace_audio",
            logo_position="top-right",
            assets=(),
            expected_duration=1.0,
        )


class PreviewComposer(StaticComposer):
    async def build(self, project_id: int, *, user_id: int | None = None, **kwargs) -> RenderPlan:
        plan = await super().build(project_id, user_id=user_id)
        return replace(plan, render_kind=str(kwargs.get("render_kind") or "final"))


class SuccessRenderer(BaseRenderer):
    def __init__(self, root: Path) -> None:
        self.root = root

    async def render(self, plan, *, render_job_id, progress_callback=None, cancel_event=None):
        await emit_progress(progress_callback, 0.45)
        output = self.root / "projects" / str(plan.project_id) / "renders" / str(render_job_id) / "output.mp4"
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"valid-render")
        await emit_progress(progress_callback, 1.0)
        return RenderResult(output_path=output, duration=1.0, file_size=output.stat().st_size, has_audio=True)


class WaitingRenderer(BaseRenderer):
    def __init__(self, root: Path) -> None:
        self.root = root

    async def render(self, plan, *, render_job_id, progress_callback=None, cancel_event=None):
        output = self.root / "projects" / str(plan.project_id) / "renders" / str(render_job_id) / "output.mp4"
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"partial")
        while cancel_event is None or not cancel_event.is_set():
            await asyncio.sleep(0.01)
        output.unlink(missing_ok=True)
        raise CancelledError("cancelled by test")


class FailOnceRenderer(BaseRenderer):
    def __init__(self, root: Path) -> None:
        self.root = root
        self.calls = 0

    async def render(self, plan, *, render_job_id, progress_callback=None, cancel_event=None):
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("synthetic ffmpeg failure")
        output = self.root / "projects" / str(plan.project_id) / "renders" / str(render_job_id) / "output.mp4"
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"ok")
        return RenderResult(output_path=output, duration=1.0, file_size=2, has_audio=True)


class TransientOnceRenderer(FailOnceRenderer):
    async def render(self, plan, *, render_job_id, progress_callback=None, cancel_event=None):
        if self.calls == 0:
            self.calls += 1
            raise FFmpegError("resource temporarily unavailable")
        return await super().render(
            plan,
            render_job_id=render_job_id,
            progress_callback=progress_callback,
            cancel_event=cancel_event,
        )


async def _project() -> tuple[int, int]:
    await init_db()
    async with SessionLocal() as session:
        user = User(telegram_id=next(_IDS), username="render-test")
        session.add(user)
        await session.flush()
        project = MediaProject(
            user_id=user.id,
            chat_id=user.telegram_id,
            name="Render Test",
            status=ProjectStatus.READY.value,
            aspect_ratio="9:16",
            width=360,
            height=640,
            fps=24,
            timeline_json='{"version":1,"template":"audio_image"}',
        )
        session.add(project)
        await session.commit()
        return user.id, project.id


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        project_dir=tmp_path / "projects",
        render_temp_dir=tmp_path / "tmp",
        render_timeout_seconds=30,
        max_project_duration_seconds=300,
        max_render_duration_seconds=300,
    )


@pytest.mark.asyncio
async def test_render_success_persists_output_and_project_status(tmp_path: Path) -> None:
    user_id, project_id = await _project()
    service = RenderService(_settings(tmp_path), composer=StaticComposer(), renderer=SuccessRenderer(tmp_path))
    job = await service.create_render(project_id, user_id=user_id)
    await service.process_render(job.id)
    stored = await service.get_render(job.id, user_id=user_id)
    assert stored is not None
    assert stored.status == RenderStatus.COMPLETED.value
    assert stored.progress == 1.0
    assert stored.output_path and Path(stored.output_path).exists()
    async with SessionLocal() as session:
        project = await session.get(MediaProject, project_id)
        assert project is not None and project.status == ProjectStatus.COMPLETED.value


@pytest.mark.asyncio
async def test_cancel_running_render_is_recoverable(tmp_path: Path) -> None:
    user_id, project_id = await _project()
    service = RenderService(_settings(tmp_path), composer=StaticComposer(), renderer=WaitingRenderer(tmp_path))
    job = await service.create_render(project_id, user_id=user_id)
    task = asyncio.create_task(service.process_render(job.id))
    for _ in range(100):
        current = await service.get_render(job.id)
        if current and current.status == RenderStatus.RENDERING.value:
            break
        await asyncio.sleep(0.01)
    assert await service.cancel_render(job.id, user_id=user_id)
    await asyncio.wait_for(task, timeout=2)
    stored = await service.get_render(job.id)
    assert stored is not None and stored.status == RenderStatus.CANCELLED.value
    async with SessionLocal() as session:
        project = await session.get(MediaProject, project_id)
        assert project is not None and project.status == ProjectStatus.READY.value


@pytest.mark.asyncio
async def test_render_queue_survives_one_renderer_failure(tmp_path: Path) -> None:
    user1, project1 = await _project()
    user2, project2 = await _project()
    renderer = FailOnceRenderer(tmp_path)
    service = RenderService(_settings(tmp_path), composer=StaticComposer(), renderer=renderer)
    first = await service.create_render(project1, user_id=user1)
    second = await service.create_render(project2, user_id=user2)
    queue = RenderQueue(
        runner=service.process_render,
        user_lookup=service.get_render_user_id,
        global_limit=1,
        per_user_limit=1,
    )
    await queue.start()
    await queue.enqueue(first.id)
    await queue.enqueue(second.id)
    # SQLite CI workers can briefly serialize the failure and completion commits.
    # Keep this a bounded wait while avoiding a scheduler-speed assertion.
    for _ in range(500):
        one = await service.get_render(first.id)
        two = await service.get_render(second.id)
        if one and two and one.status == RenderStatus.FAILED.value and two.status == RenderStatus.COMPLETED.value:
            break
        await asyncio.sleep(0.02)
    await queue.shutdown()
    one = await service.get_render(first.id)
    two = await service.get_render(second.id)
    assert one is not None and one.status == RenderStatus.FAILED.value
    assert two is not None and two.status == RenderStatus.COMPLETED.value


@pytest.mark.asyncio
async def test_render_retries_only_transient_ffmpeg_failure(tmp_path: Path, monkeypatch) -> None:
    user_id, project_id = await _project()
    renderer = TransientOnceRenderer(tmp_path)
    settings = _settings(tmp_path)
    settings.max_render_retries = 1
    service = RenderService(settings, composer=StaticComposer(), renderer=renderer)

    async def no_sleep(_delay):
        return None

    monkeypatch.setattr("app.services.render_service.asyncio.sleep", no_sleep)
    job = await service.create_render(project_id, user_id=user_id)
    await service.process_render(job.id)
    stored = await service.get_render(job.id)
    assert stored is not None and stored.status == RenderStatus.COMPLETED.value
    assert renderer.calls == 2


@pytest.mark.asyncio
async def test_preview_render_is_persisted_without_completing_project(tmp_path: Path) -> None:
    user_id, project_id = await _project()
    service = RenderService(
        _settings(tmp_path), composer=PreviewComposer(), renderer=SuccessRenderer(tmp_path)
    )
    job = await service.create_render(
        project_id,
        user_id=user_id,
        kind="preview",
        preview_duration=10,
        preview_width=480,
    )
    assert await service.get_render_kind(job.id) == "preview"
    await service.process_render(job.id)
    stored = await service.get_render(job.id)
    assert stored is not None and stored.status == RenderStatus.COMPLETED.value
    async with SessionLocal() as session:
        project = await session.get(MediaProject, project_id)
        assert project is not None and project.status == ProjectStatus.READY.value


@pytest.mark.asyncio
async def test_startup_reconciliation_requeues_only_queued_and_fails_interrupted(tmp_path: Path) -> None:
    user_id, project_id = await _project()
    async with SessionLocal() as session:
        queued = RenderJob(project_id=project_id, user_id=user_id, status=RenderStatus.QUEUED.value)
        interrupted = RenderJob(project_id=project_id, user_id=user_id, status=RenderStatus.RENDERING.value)
        session.add_all([queued, interrupted])
        await session.commit()
        await session.refresh(queued)
        await session.refresh(interrupted)
        queued_id, interrupted_id = queued.id, interrupted.id
    service = RenderService(_settings(tmp_path), composer=StaticComposer(), renderer=SuccessRenderer(tmp_path))
    ids = await service.reconcile_after_restart()
    assert queued_id in ids
    recovered = await service.get_render(interrupted_id)
    assert recovered is not None
    assert recovered.status == RenderStatus.FAILED.value
    assert recovered.error and "RESTART_INTERRUPTED" in recovered.error


@pytest.mark.asyncio
async def test_cancel_queued_render_is_terminal_and_recoverable(tmp_path: Path) -> None:
    user_id, project_id = await _project()
    service = RenderService(_settings(tmp_path), composer=StaticComposer(), renderer=SuccessRenderer(tmp_path))
    job = await service.create_render(project_id, user_id=user_id)
    assert await service.cancel_render(job.id, user_id=user_id)
    stored = await service.get_render(job.id)
    assert stored is not None and stored.status == RenderStatus.CANCELLED.value
    async with SessionLocal() as session:
        project = await session.get(MediaProject, project_id)
        assert project is not None and project.status == ProjectStatus.READY.value


@pytest.mark.asyncio
async def test_render_queue_rejects_duplicate_and_enforces_per_user_limit() -> None:
    release = asyncio.Event()
    active_by_user: dict[int, int] = {}
    peak_by_user: dict[int, int] = {}
    users = {1: 10, 2: 10, 3: 20}

    async def lookup(render_id: int) -> int:
        return users[render_id]

    async def runner(render_id: int) -> None:
        user_id = users[render_id]
        active_by_user[user_id] = active_by_user.get(user_id, 0) + 1
        peak_by_user[user_id] = max(peak_by_user.get(user_id, 0), active_by_user[user_id])
        await release.wait()
        active_by_user[user_id] -= 1

    async def canceller(render_id: int, user_id: int | None) -> bool:
        return True

    queue = RenderQueue(
        runner=runner,
        user_lookup=lookup,
        global_limit=2,
        per_user_limit=1,
        canceller=canceller,
        cancel_requester=lambda render_id: True,
    )
    await queue.start()
    assert await queue.enqueue(1)
    assert not await queue.enqueue(1)
    assert await queue.enqueue(2)
    assert await queue.enqueue(3)
    for _ in range(100):
        if len(queue.active_render_ids) == 2:
            break
        await asyncio.sleep(0.01)
    assert set(queue.active_render_ids) == {1, 3}
    assert peak_by_user == {10: 1, 20: 1}
    release.set()
    for _ in range(100):
        if not queue.active_render_ids:
            break
        await asyncio.sleep(0.01)
    await queue.shutdown()


@pytest.mark.asyncio
async def test_restart_preserves_valid_upload_output_and_scopes_workspace_cleanup(tmp_path: Path) -> None:
    user_id, project_id = await _project()
    settings = _settings(tmp_path)
    async with SessionLocal() as session:
        job = RenderJob(project_id=project_id, user_id=user_id, status=RenderStatus.UPLOADING.value)
        session.add(job)
        await session.flush()
        output = settings.project_dir / str(project_id) / "renders" / str(job.id) / "output.mp4"
        output.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-f",
                "lavfi",
                "-i",
                "color=c=black:s=160x90:d=0.5",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                str(output),
            ],
            check=True,
            capture_output=True,
        )
        job.output_path = str(output)
        await session.commit()
        job_id = job.id
    stale = settings.render_temp_dir / f"render-{job_id}"
    unrelated = settings.render_temp_dir / "telegram-upload-live"
    stale.mkdir(parents=True)
    unrelated.mkdir(parents=True)
    service = RenderService(settings, composer=StaticComposer(), renderer=SuccessRenderer(tmp_path))
    await service.reconcile_after_restart()
    stored = await service.get_render(job_id)
    assert stored is not None and stored.status == RenderStatus.COMPLETED.value
    assert stored.error and "DELIVERY_INTERRUPTED" in stored.error
    assert not stale.exists()
    assert unrelated.exists()


@pytest.mark.asyncio
async def test_cancelling_project_cancels_active_render_and_preserves_project_cancellation(
    tmp_path: Path,
) -> None:
    user_id, project_id = await _project()
    settings = _settings(tmp_path)
    service = RenderService(settings, composer=StaticComposer(), renderer=WaitingRenderer(tmp_path))
    job = await service.create_render(project_id, user_id=user_id)
    task = asyncio.create_task(service.process_render(job.id))
    for _ in range(100):
        current = await service.get_render(job.id)
        if current and current.status == RenderStatus.RENDERING.value:
            break
        await asyncio.sleep(0.01)
    assert await ProjectService(settings).cancel_project(project_id, user_id=user_id)
    assert await service.cancel_project_renders(project_id, user_id=user_id) == 1
    await asyncio.wait_for(task, timeout=2)
    stored = await service.get_render(job.id)
    assert stored is not None and stored.status == RenderStatus.CANCELLED.value
    async with SessionLocal() as session:
        project = await session.get(MediaProject, project_id)
        assert project is not None and project.status == ProjectStatus.CANCELLED.value
