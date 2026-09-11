from __future__ import annotations

import itertools
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter

from app import db as database
from app.bot import studio
from app.config import Settings
from app.services.assets import AssetService
from app.services.composer import ComposerService
from app.services.downloader import MediaInfo
from app.services.projects import ProjectService
from app.services.render_service import RenderService
from app.services.renderers.ffmpeg_renderer import FFmpegRenderer

_TELEGRAM_IDS = itertools.count(7_770_000)


def _ffmpeg(*args: str) -> None:
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args],
        check=True,
        capture_output=True,
    )


def _image(path: Path) -> Path:
    _ffmpeg("-f", "lavfi", "-i", "color=c=green:s=160x90", "-frames:v", "1", str(path))
    return path


def _audio(path: Path, *, codec: str = "libmp3lame") -> Path:
    _ffmpeg("-f", "lavfi", "-i", "sine=frequency=550:duration=1", "-c:a", codec, str(path))
    return path


def _video(path: Path) -> Path:
    _ffmpeg(
        "-f",
        "lavfi",
        "-i",
        "testsrc=size=160x90:rate=15:duration=1",
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=440:duration=1",
        "-shortest",
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        str(path),
    )
    return path


def _callbacks(markup) -> set[str]:
    return {
        button.callback_data
        for row in markup.inline_keyboard
        for button in row
        if button.callback_data
    }


def test_studio_home_and_collection_expose_guided_workflows() -> None:
    home = _callbacks(studio.studio_home_keyboard())
    assert {
        "studio:workflow:smart_edit",
        "studio:workflow:cut",
        "studio:workflow:audio",
        "studio:workflow:captions",
        "studio:reopen",
        "studio:projects",
    } <= home
    collecting = _callbacks(studio.collecting_keyboard(17))
    assert "studio:done:17" in collecting
    assert "studio:summary:17" in collecting


def test_cut_and_audio_workflows_expose_only_supported_operations() -> None:
    cut = _callbacks(studio.workflow_instruction_keyboard(17, "cut"))
    assert {
        "studio:operation:17:trim",
        "studio:operation:17:remove_range",
        "studio:operation:17:split_30",
        "studio:operation:17:split_60",
        "studio:operation:17:split_scenes",
        "studio:operation:17:remove_silence",
        "studio:operation:17:best_moment",
    } <= cut
    audio = _callbacks(studio.workflow_instruction_keyboard(17, "audio"))
    assert {
        "studio:operation:17:mute_original",
        "studio:operation:17:replace_audio",
        "studio:operation:17:mix_audio",
        "studio:operation:17:auto_duck",
        "studio:operation:17:normalize",
    } <= audio
    assert not any("split" in value for value in audio)


def test_feedback_controls_keep_project_context_and_revision_actions() -> None:
    actions = _callbacks(studio.agent_keyboard(43))
    assert {
        "studio:agentrender:43:preview",
        "studio:agentrender:43:final",
        "studio:agentundo:43",
        "studio:agentredo:43",
        "studio:add:43",
        "studio:summary:43",
    } <= actions


async def _user_project(settings: Settings) -> tuple[database.User, database.MediaProject]:
    await database.init_db()
    async with database.SessionLocal() as session:
        user = database.User(telegram_id=next(_TELEGRAM_IDS), username="studio")
        session.add(user)
        await session.flush()
        project = database.MediaProject(
            user_id=user.id,
            chat_id=user.telegram_id,
            name="Telegram Studio",
            width=360,
            height=640,
            fps=24,
        )
        session.add(project)
        await session.commit()
        await session.refresh(user)
        await session.refresh(project)
        session.expunge_all()
        return user, project


class FakeState:
    def __init__(self, project_id: int) -> None:
        self.project_id = project_id
        self.data = {
            "studio_project_id": project_id,
            "studio_phase": "COLLECTING_MEDIA",
            "studio_workflow": "smart_edit",
        }

    async def get_data(self):
        return dict(self.data)

    async def update_data(self, **values):
        self.data.update(values)

    async def clear(self):
        self.project_id = 0
        self.data.clear()


class DownloadBot:
    def __init__(self, files: dict[str, Path]) -> None:
        self.files = files

    async def download(self, file_id: str, *, destination: Path) -> None:
        shutil.copyfile(self.files[file_id], destination)


class FakeMessage:
    def __init__(self, bot, payload_type: str, payload) -> None:
        self.bot = bot
        self.from_user = SimpleNamespace(id=123, username="tester")
        self.video = payload if payload_type == "video" else None
        self.photo = [payload] if payload_type == "photo" else None
        self.audio = payload if payload_type == "audio" else None
        self.voice = payload if payload_type == "voice" else None
        self.document = payload if payload_type == "document" else None
        self.answers: list[str] = []

    async def answer(self, text: str, **kwargs):
        self.answers.append(text)
        return SimpleNamespace(message_id=len(self.answers))


@pytest.mark.asyncio
async def test_safe_edit_treats_message_not_modified_as_success() -> None:
    class Bot:
        async def edit_message_text(self, **kwargs):
            raise TelegramBadRequest(method=SimpleNamespace(), message="message is not modified")

    assert await studio._safe_edit(Bot(), 1, 2, "same") is True


@pytest.mark.asyncio
async def test_safe_edit_retries_once_after_rate_limit(monkeypatch) -> None:
    calls = 0

    class Bot:
        async def edit_message_text(self, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise TelegramRetryAfter(
                    method=SimpleNamespace(), message="retry", retry_after=1
                )
            return True

    async def no_sleep(_delay):
        return None

    monkeypatch.setattr(studio.asyncio, "sleep", no_sleep)
    assert await studio._safe_edit(Bot(), 1, 2, "updated") is True
    assert calls == 2


@pytest.mark.asyncio
async def test_safe_bound_edit_treats_message_not_modified_as_success() -> None:
    class Message:
        async def edit_text(self, *args, **kwargs):
            raise TelegramBadRequest(
                method=SimpleNamespace(), message="message is not modified"
            )

    assert await studio._safe_bound_edit(Message(), "same") is True


@pytest.mark.asyncio
async def test_video_audio_button_applies_owned_timeline_mute(monkeypatch) -> None:
    shown = []

    class Callback:
        data = "studio:audio:12:34:mute"
        message = SimpleNamespace()

        async def answer(self, text=None, **kwargs):
            return None

    async def owned(_callback, project_id):
        assert project_id == 12
        return SimpleNamespace(id=7), SimpleNamespace(id=12)

    async def assets(project_id, *, user_id):
        return [
            SimpleNamespace(
                asset=SimpleNamespace(id=34, asset_type="video"),
                link=SimpleNamespace(role="main"),
            )
        ]

    async def capture_apply(project_id, *, user_id, calls: list):
        calls_copy = list(calls)
        assert (project_id, user_id) == (12, 7)
        captured.extend(calls_copy)
        return SimpleNamespace()

    async def show(message, project_id, user_id):
        shown.append((project_id, user_id))

    captured: list[dict] = []
    monkeypatch.setattr(studio, "_owned_project", owned)
    monkeypatch.setattr(studio.project_service, "list_assets", assets)
    monkeypatch.setattr(studio.timeline_service, "apply", capture_apply)
    monkeypatch.setattr(studio, "_show_assets", show)
    await studio.set_video_original_audio(Callback())
    assert captured == [
        {
            "name": "set_original_audio",
            "arguments": {"clip_id": "asset-34", "enabled": False},
        }
    ]
    assert shown == [(12, 7)]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("payload_type", "filename", "mime", "declared"),
    [
        ("video", "clip.mp4", "video/mp4", "video"),
        ("photo", "photo.jpg", "image/jpeg", "image"),
        ("audio", "track.mp3", "audio/mpeg", "audio"),
        ("voice", "voice.ogg", "audio/ogg", "voice"),
    ],
)
async def test_telegram_upload_ingestion(
    tmp_path: Path,
    monkeypatch,
    payload_type: str,
    filename: str,
    mime: str,
    declared: str,
) -> None:
    settings = Settings(project_dir=tmp_path / "projects", render_temp_dir=tmp_path / "tmp")
    user, project = await _user_project(settings)
    source = tmp_path / filename
    if declared == "video":
        _video(source)
    elif declared == "image":
        _image(source)
    elif declared == "voice":
        _audio(source, codec="libopus")
    else:
        _audio(source)
    payload = SimpleNamespace(
        file_id=f"id-{payload_type}",
        file_name=filename,
        mime_type=mime,
        file_size=source.stat().st_size,
    )
    message = FakeMessage(DownloadBot({payload.file_id: source}), payload_type, payload)

    async def current_user(_message):
        return user

    monkeypatch.setattr(studio, "settings", settings)
    monkeypatch.setattr(studio, "_message_user", current_user)
    monkeypatch.setattr(studio, "_schedule_project_summary", lambda *args, **kwargs: None)
    assets = AssetService(settings)
    projects = ProjectService(settings)
    state = FakeState(project.id)
    await studio.ingest_upload_message(
        message,
        state,
        assets=assets,
        projects=projects,
    )
    linked = await projects.list_assets(project.id, user_id=user.id)
    assert len(linked) == 1
    assert linked[0].asset.asset_type == declared
    assert Path(linked[0].asset.local_path).is_file()
    assert state.data["recent_asset_id"] == linked[0].asset.id
    assert not any("تعذر قبول الملف" in answer for answer in message.answers)
    assert not list(settings.render_temp_dir.glob("telegram-upload-*"))


@pytest.mark.asyncio
async def test_document_rejection_does_not_leave_asset_or_temp_file(tmp_path: Path, monkeypatch) -> None:
    settings = Settings(project_dir=tmp_path / "projects", render_temp_dir=tmp_path / "tmp")
    user, project = await _user_project(settings)
    source = tmp_path / "payload.txt"
    source.write_text("not media", encoding="utf-8")
    payload = SimpleNamespace(
        file_id="doc-id",
        file_name="payload.txt",
        mime_type="text/plain",
        file_size=source.stat().st_size,
    )
    message = FakeMessage(DownloadBot({"doc-id": source}), "document", payload)

    async def current_user(_message):
        return user

    monkeypatch.setattr(studio, "settings", settings)
    monkeypatch.setattr(studio, "_message_user", current_user)
    projects = ProjectService(settings)
    await studio.ingest_upload_message(
        message,
        FakeState(project.id),
        assets=AssetService(settings),
        projects=projects,
    )
    assert await projects.list_assets(project.id, user_id=user.id) == []
    assert "تعذر قبول الملف" in message.answers[-1]
    assert not list(settings.render_temp_dir.glob("telegram-upload-*"))


class FakeDownloader:
    def __init__(self, source: Path) -> None:
        self.source = source
        self.forgotten: list[str] = []

    def probe(self, url: str) -> MediaInfo:
        return MediaInfo(
            title="Downloaded clip",
            thumbnail=None,
            duration=1,
            uploader="fixture",
            platform="generic",
            webpage_url=url,
            qualities=[360],
        )

    def download(self, url, quality, workspace, **kwargs):
        target = workspace / "download.mp4"
        shutil.copyfile(self.source, target)
        return target

    def forget(self, job_key: str) -> None:
        self.forgotten.append(job_key)


@pytest.mark.asyncio
async def test_url_download_becomes_project_asset_with_existing_downloader_contract(tmp_path: Path) -> None:
    settings = Settings(project_dir=tmp_path / "projects", render_temp_dir=tmp_path / "tmp")
    user, project = await _user_project(settings)
    source = _video(tmp_path / "source.mp4")
    downloader = FakeDownloader(source)
    assets = AssetService(settings, downloader=downloader)  # type: ignore[arg-type]
    stored = await assets.ingest_url(
        "https://media.example/watch/1",
        user_id=user.id,
        project_id=project.id,
    )
    projects = ProjectService(settings)
    await projects.add_asset(project.id, stored.id, user_id=user.id)
    uploaded = await assets.ingest_file(
        _image(tmp_path / "uploaded.jpg"),
        user_id=user.id,
        project_id=project.id,
        declared_type="image",
        source_type="telegram",
        telegram_file_id="telegram-photo-id",
    )
    await projects.add_asset(project.id, uploaded.id, user_id=user.id)
    linked = await projects.list_assets(project.id, user_id=user.id)
    assert {item.asset.source_type for item in linked} == {"url", "telegram"}
    assert linked[0].asset.source_url == "https://media.example/watch/1"
    assert downloader.forgotten
    assert not list(settings.render_temp_dir.glob("ingest-*"))


class DeliveryBot:
    def __init__(self) -> None:
        self.videos = []
        self.edits = []

    async def edit_message_text(self, **kwargs):
        self.edits.append(kwargs)

    async def send_video(self, **kwargs):
        self.videos.append(kwargs)

    async def send_message(self, *args, **kwargs):
        raise AssertionError("delivery should not fail")


@pytest.mark.asyncio
async def test_mocked_telegram_project_render_and_delivery_e2e(tmp_path: Path, monkeypatch) -> None:
    settings = Settings(
        project_dir=tmp_path / "projects",
        render_temp_dir=tmp_path / "tmp",
        max_project_duration_seconds=60,
        max_render_duration_seconds=60,
        render_timeout_seconds=60,
        telegram_upload_limit_mb=10,
    )
    user, project = await _user_project(settings)
    projects = ProjectService(settings)
    assets = AssetService(settings)
    image = await assets.ingest_file(
        _image(tmp_path / "cover.jpg"),
        user_id=user.id,
        project_id=project.id,
        declared_type="image",
    )
    audio = await assets.ingest_file(
        _audio(tmp_path / "sound.mp3"),
        user_id=user.id,
        project_id=project.id,
        declared_type="audio",
    )
    await projects.add_asset(project.id, image.id, user_id=user.id)
    await projects.add_asset(project.id, audio.id, user_id=user.id)
    await projects.set_preset(project.id, user_id=user.id, preset="vertical")
    await projects.apply_template(project.id, user_id=user.id, template="audio_image")
    composer = ComposerService(settings, projects=projects)
    service = RenderService(settings, composer=composer, renderer=FFmpegRenderer(settings))
    job = await service.create_render(project.id, user_id=user.id)
    await service.process_render(job.id)
    bot = DeliveryBot()
    monkeypatch.setattr(studio, "settings", settings)
    await studio.watch_render_and_deliver(
        bot,  # type: ignore[arg-type]
        chat_id=project.chat_id,
        message_id=99,
        render_job_id=job.id,
        user_id=user.id,
        service=service,
    )
    stored = await service.get_render(job.id, user_id=user.id)
    assert stored is not None and stored.status == database.RenderStatus.COMPLETED.value
    assert len(bot.videos) == 1
    assert bot.edits[-1]["text"].startswith("✅ اكتمل")


@pytest.mark.asyncio
async def test_callback_project_ownership_is_enforced(tmp_path: Path, monkeypatch) -> None:
    settings = Settings(project_dir=tmp_path / "projects", render_temp_dir=tmp_path / "tmp")
    owner, project = await _user_project(settings)
    attacker, _ = await _user_project(settings)
    answers: list[tuple[str, bool]] = []
    callback = SimpleNamespace(
        from_user=SimpleNamespace(id=attacker.telegram_id),
        answer=lambda *args, **kwargs: None,
    )

    async def answer(text: str, show_alert: bool = False):
        answers.append((text, show_alert))

    async def callback_user(_callback):
        return attacker

    callback.answer = answer
    monkeypatch.setattr(studio, "_callback_user", callback_user)
    monkeypatch.setattr(studio, "project_service", ProjectService(settings))
    assert await studio._owned_project(callback, project.id) is None
    assert answers == [("المشروع غير موجود", True)]
    assert owner.id != attacker.id


class FailingDeliveryBot(DeliveryBot):
    def __init__(self) -> None:
        super().__init__()
        self.messages: list[str] = []

    async def send_video(self, **kwargs):
        raise RuntimeError("temporary Telegram outage")

    async def send_message(self, chat_id, text):
        self.messages.append(text)


@pytest.mark.asyncio
async def test_delivery_failure_does_not_turn_successful_render_into_failed(
    tmp_path: Path,
    monkeypatch,
) -> None:
    settings = Settings(
        project_dir=tmp_path / "projects",
        render_temp_dir=tmp_path / "tmp",
        telegram_upload_limit_mb=10,
    )
    user, project = await _user_project(settings)
    async with database.SessionLocal() as session:
        job = database.RenderJob(
            project_id=project.id,
            user_id=user.id,
            status=database.RenderStatus.COMPLETED.value,
            progress=1.0,
        )
        session.add(job)
        await session.flush()
        output = settings.project_dir / str(project.id) / "renders" / str(job.id) / "output.mp4"
        output.parent.mkdir(parents=True)
        _video(output)
        job.output_path = str(output)
        job.file_size = output.stat().st_size
        await session.commit()
        job_id = job.id
    service = RenderService(settings)
    bot = FailingDeliveryBot()
    monkeypatch.setattr(studio, "settings", settings)
    await studio.watch_render_and_deliver(
        bot,  # type: ignore[arg-type]
        chat_id=project.chat_id,
        message_id=88,
        render_job_id=job_id,
        user_id=user.id,
        service=service,
    )
    stored = await service.get_render(job_id, user_id=user.id)
    assert stored is not None and stored.status == database.RenderStatus.COMPLETED.value
    assert stored.error and stored.error.startswith("DELIVERY_FAILED:")
    assert bot.messages and "تم إنشاء الفيديو بنجاح" in bot.messages[0]
