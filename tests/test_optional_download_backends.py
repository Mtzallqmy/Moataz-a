from __future__ import annotations

import sys
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.config import Settings
from app.errors import ErrorCode, classify_error
from app.services.download_backends.base import DownloadRequest
from app.services.download_backends.gallerydl_backend import GalleryDlBackend
from app.services.download_backends.instaloader_backend import (
    InstagramSessionRequiredError,
    InstaloaderBackend,
)
from app.services.download_backends.pytubefix_backend import PytubefixBackend
from app.services.download_backends.tiktok_backend import TikTokBackend


@pytest.mark.asyncio
async def test_optional_backends_missing_or_disabled_do_not_break_startup(monkeypatch):
    import app.services.download_backends.gallerydl_backend as gallery_module
    import app.services.download_backends.instaloader_backend as instaloader_module

    monkeypatch.setattr(gallery_module.shutil, "which", lambda _name: None)
    monkeypatch.setattr(instaloader_module.importlib.util, "find_spec", lambda _name: None)
    settings = Settings(
        gallerydl_enabled=True,
        instaloader_enabled=True,
        pytubefix_enabled=True,
        tiktok_backend_enabled=False,
    )

    assert not await GalleryDlBackend(settings).available()
    assert not await InstaloaderBackend(settings).available()
    assert not await PytubefixBackend(settings).available()
    assert not await TikTokBackend(settings).available()


@pytest.mark.asyncio
async def test_gallerydl_uses_argument_array_and_validates_output(monkeypatch, tmp_path: Path):
    import app.services.download_backends.gallerydl_backend as gallery_module

    captured: dict[str, object] = {}

    class FakeProcess:
        returncode = 0

        def __init__(self, cwd: Path) -> None:
            self.cwd = cwd

        async def communicate(self):
            (self.cwd / "image.jpg").write_bytes(b"\xff\xd8\xff" + b"image-data")
            return b"ok", b""

    async def fake_create_subprocess_exec(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return FakeProcess(Path(kwargs["cwd"]))

    monkeypatch.setattr(gallery_module.shutil, "which", lambda _name: "/usr/bin/gallery-dl")
    monkeypatch.setattr(gallery_module.asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    backend = GalleryDlBackend(Settings(gallerydl_enabled=True))

    result = await backend.download(
        DownloadRequest(
            "https://www.instagram.com/p/example/",
            tmp_path,
            media_type="images",
        )
    )

    args = captured["args"]
    kwargs = captured["kwargs"]
    assert isinstance(args, tuple)
    assert args[0] == "/usr/bin/gallery-dl"
    assert "--destination" in args
    assert args[args.index("--destination") + 1] == str(tmp_path.resolve())
    assert "shell" not in kwargs
    assert result.files == [tmp_path / "image.jpg"]


@pytest.mark.asyncio
async def test_gallerydl_cancellation_terminates_and_cleans_partial(monkeypatch, tmp_path: Path):
    import app.services.download_backends.gallerydl_backend as gallery_module

    class FakeProcess:
        returncode = None

        def __init__(self, cwd: Path) -> None:
            self.cwd = cwd
            self.finished = __import__("asyncio").Event()
            self.terminated = False

        async def communicate(self):
            (self.cwd / "media.part").write_bytes(b"partial")
            await self.finished.wait()
            return b"", b""

        def terminate(self) -> None:
            self.terminated = True
            self.returncode = -15
            self.finished.set()

        def kill(self) -> None:
            self.returncode = -9
            self.finished.set()

        async def wait(self):
            await self.finished.wait()
            return self.returncode

    holder: dict[str, FakeProcess] = {}

    async def fake_create_subprocess_exec(*_args, **kwargs):
        process = FakeProcess(Path(kwargs["cwd"]))
        holder["process"] = process
        return process

    monkeypatch.setattr(gallery_module.shutil, "which", lambda _name: "/usr/bin/gallery-dl")
    monkeypatch.setattr(gallery_module.asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    cancel_event = threading.Event()
    cancel_event.set()
    backend = GalleryDlBackend(Settings(gallerydl_enabled=True))

    with pytest.raises(Exception, match="cancelled") as caught:
        await backend.download(
            DownloadRequest(
                "https://www.instagram.com/stories/user/123/",
                tmp_path,
                media_type="story",
                cancel_event=cancel_event,
            )
        )

    assert classify_error(caught.value).code is ErrorCode.CANCELLED
    assert holder["process"].terminated
    assert not (tmp_path / "media.part").exists()


class _FakeInstaloader:
    def __init__(self, **_kwargs) -> None:
        self.context = object()
        self.loaded_session: tuple[str, str] | None = None
        self.story_media_id: int | None = None

    def load_session_from_file(self, username: str, path: str) -> None:
        self.loaded_session = (username, path)

    def download_post(self, _post, target: Path) -> None:
        Path(target, "first.jpg").write_bytes(b"one")
        Path(target, "second.jpg").write_bytes(b"two")

    def download_storyitem(self, item, target: Path) -> None:
        self.story_media_id = item.media_id
        Path(target, "story.jpg").write_bytes(b"story")


def _install_fake_instaloader(monkeypatch, loader: _FakeInstaloader) -> None:
    class FakePost:
        @staticmethod
        def from_shortcode(_context, shortcode: str):
            return SimpleNamespace(
                shortcode=shortcode,
                is_video=False,
                caption="carousel",
                video_duration=None,
                mediaid=42,
            )

    class FakeStoryItem:
        @staticmethod
        def from_mediaid(_context, media_id: int):
            return SimpleNamespace(media_id=media_id)

    module = SimpleNamespace(
        Instaloader=lambda **_kwargs: loader,
        Post=FakePost,
        StoryItem=FakeStoryItem,
    )
    monkeypatch.setitem(sys.modules, "instaloader", module)


@pytest.mark.asyncio
async def test_instaloader_carousel_downloads_only_new_media(monkeypatch, tmp_path: Path):
    loader = _FakeInstaloader()
    _install_fake_instaloader(monkeypatch, loader)
    backend = InstaloaderBackend(Settings(instaloader_enabled=True))
    monkeypatch.setattr(backend, "available", AsyncMock(return_value=True))
    (tmp_path / "existing.jpg").write_bytes(b"existing")

    result = await backend.download(
        DownloadRequest(
            "https://www.instagram.com/p/carousel123/",
            tmp_path,
            media_type="images",
        )
    )

    assert [path.name for path in result.files] == ["first.jpg", "second.jpg"]
    assert (tmp_path / "existing.jpg").exists()


@pytest.mark.asyncio
async def test_instaloader_story_requires_authorized_session(monkeypatch, tmp_path: Path):
    loader = _FakeInstaloader()
    _install_fake_instaloader(monkeypatch, loader)
    backend = InstaloaderBackend(Settings(instaloader_enabled=True))
    monkeypatch.setattr(backend, "available", AsyncMock(return_value=True))

    with pytest.raises(InstagramSessionRequiredError) as caught:
        await backend.download(
            DownloadRequest(
                "https://www.instagram.com/stories/example/123456/",
                tmp_path,
                media_type="story",
            )
        )

    assert classify_error(caught.value).code is ErrorCode.AUTH_REQUIRED
    assert loader.loaded_session is None


@pytest.mark.asyncio
async def test_instaloader_story_uses_single_item_session_path(monkeypatch, tmp_path: Path):
    loader = _FakeInstaloader()
    _install_fake_instaloader(monkeypatch, loader)
    session_file = tmp_path / "session-demo"
    session_file.write_text("opaque-session", encoding="utf-8")
    backend = InstaloaderBackend(
        Settings(instaloader_enabled=True, instagram_session_file=session_file)
    )
    monkeypatch.setattr(backend, "available", AsyncMock(return_value=True))
    output_dir = tmp_path / "job"

    result = await backend.download(
        DownloadRequest(
            "https://www.instagram.com/stories/example/123456/",
            output_dir,
            media_type="story",
        )
    )

    assert loader.loaded_session == ("demo", str(session_file.resolve()))
    assert loader.story_media_id == 123456
    assert [path.name for path in result.files] == ["story.jpg"]


class _FakeStream:
    def __init__(
        self,
        *,
        itag: int,
        resolution: str | None = None,
        fps: int = 30,
        abr: str | None = None,
        subtype: str = "mp4",
    ) -> None:
        self.itag = itag
        self.resolution = resolution
        self.fps = fps
        self.abr = abr
        self.subtype = subtype
        self.downloads = 0

    def download(self, *, output_path: str, filename: str, **_kwargs) -> str:
        self.downloads += 1
        path = Path(output_path) / filename
        path.write_bytes(b"media")
        return str(path)


class _FakeStreams:
    def __init__(self, progressive, adaptive, audio) -> None:
        self.progressive = progressive
        self.adaptive = adaptive
        self.audio = audio

    def filter(self, **kwargs):
        if kwargs.get("only_audio"):
            return self.audio
        if kwargs.get("only_video"):
            return self.adaptive
        if kwargs.get("progressive"):
            return self.progressive
        if kwargs.get("type") == "video":
            return [*self.progressive, *self.adaptive]
        return []


@pytest.mark.asyncio
async def test_pytubefix_selects_requested_stream_deterministically(monkeypatch, tmp_path: Path):
    import app.services.download_backends.pytubefix_backend as pytubefix_module

    low = _FakeStream(itag=18, resolution="360p")
    high = _FakeStream(itag=22, resolution="720p", fps=60)
    youtube = SimpleNamespace(
        streams=_FakeStreams([low, high], [], []),
        check_availability=lambda: None,
    )
    monkeypatch.setitem(sys.modules, "pytubefix", SimpleNamespace(YouTube=lambda _url: youtube))
    backend = PytubefixBackend(Settings(pytubefix_enabled=True))
    monkeypatch.setattr(backend, "available", AsyncMock(return_value=True))
    monkeypatch.setattr(pytubefix_module, "probe_media_file", AsyncMock(return_value=None))

    result = await backend.download(
        DownloadRequest(
            "https://www.youtube.com/watch?v=example",
            tmp_path,
            quality="720",
        )
    )

    assert result.primary_file.name == "pytubefix-video.mp4"
    assert high.downloads == 1
    assert low.downloads == 0


@pytest.mark.asyncio
async def test_pytubefix_audio_transcodes_and_cleans_intermediate(monkeypatch, tmp_path: Path):
    import app.services.download_backends.pytubefix_backend as pytubefix_module

    low = _FakeStream(itag=140, abr="128kbps", subtype="m4a")
    high = _FakeStream(itag=251, abr="192kbps", subtype="m4a")
    youtube = SimpleNamespace(
        streams=_FakeStreams([], [], [low, high]),
        check_availability=lambda: None,
    )
    monkeypatch.setitem(sys.modules, "pytubefix", SimpleNamespace(YouTube=lambda _url: youtube))
    backend = PytubefixBackend(Settings(pytubefix_enabled=True))
    monkeypatch.setattr(backend, "available", AsyncMock(return_value=True))

    async def fake_run_process(*args, **_kwargs):
        Path(args[-1]).write_bytes(b"mp3")
        return b"", b""

    monkeypatch.setattr(pytubefix_module, "_run_process", fake_run_process)
    monkeypatch.setattr(pytubefix_module, "probe_media_file", AsyncMock(return_value=None))

    result = await backend.download(
        DownloadRequest(
            "https://www.youtube.com/watch?v=audio",
            tmp_path,
            media_type="audio",
            quality="mp3",
        )
    )

    assert result.primary_file.name == "pytubefix-audio.mp3"
    assert high.downloads == 1
    assert low.downloads == 0
    assert not (tmp_path / "pytubefix-audio.m4a").exists()


@pytest.mark.asyncio
async def test_pytubefix_playlist_metadata_uses_safe_limit(monkeypatch):
    backend = PytubefixBackend(Settings(pytubefix_enabled=True, max_playlist_items=3))
    monkeypatch.setattr(backend, "available", AsyncMock(return_value=True))
    monkeypatch.setitem(
        sys.modules,
        "pytubefix",
        SimpleNamespace(
            Playlist=lambda _url: SimpleNamespace(
                video_urls=["https://youtu.be/one", "https://youtu.be/two"]
            )
        ),
    )

    entries = await backend.expand_playlist("https://youtube.com/playlist?list=abc", limit=3)

    assert [entry["index"] for entry in entries] == [1, 2]
    assert [entry["url"] for entry in entries] == ["https://youtu.be/one", "https://youtu.be/two"]


class _FakeContent:
    def __init__(self, data: bytes) -> None:
        self.data = data

    async def iter_chunked(self, _size: int):
        yield self.data


class _FakeResponse:
    def __init__(self, status: int, data: bytes = b"", payload: dict | None = None) -> None:
        self.status = status
        self.content = _FakeContent(data)
        self.payload = payload or {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, _exc_type, _exc, _tb):
        return False

    async def json(self):
        return self.payload


class _FakeClientSession:
    def __init__(self, *, media: dict[str, bytes] | None = None, post_status: int = 200, **_kwargs) -> None:
        self.media = media or {}
        self.post_status = post_status

    async def __aenter__(self):
        return self

    async def __aexit__(self, _exc_type, _exc, _tb):
        return False

    def get(self, url: str):
        return _FakeResponse(200, self.media[url])

    def post(self, _url: str, **_kwargs):
        return _FakeResponse(self.post_status, payload={"media": []})


@pytest.mark.parametrize(
    ("media_type", "items", "media_bytes", "expected_names"),
    [
        (
            "video",
            [{"url": "https://cdn.example/video", "kind": "video"}],
            {"https://cdn.example/video": b"video"},
            ["tiktok-001.mp4"],
        ),
        (
            "audio",
            [{"url": "https://cdn.example/audio", "kind": "audio"}],
            {"https://cdn.example/audio": b"audio"},
            ["tiktok-001.m4a"],
        ),
        (
            "images",
            [
                {"url": "https://cdn.example/one", "kind": "image"},
                {"url": "https://cdn.example/two", "kind": "image"},
            ],
            {
                "https://cdn.example/one": b"\xff\xd8\xffone",
                "https://cdn.example/two": b"\xff\xd8\xfftwo",
            },
            ["tiktok-001.jpg", "tiktok-002.jpg"],
        ),
    ],
)
@pytest.mark.asyncio
async def test_tiktok_sidecar_downloads_video_audio_and_slideshow(
    monkeypatch,
    tmp_path: Path,
    media_type: str,
    items: list[dict[str, str]],
    media_bytes: dict[str, bytes],
    expected_names: list[str],
):
    import app.services.download_backends.tiktok_backend as tiktok_module

    backend = TikTokBackend(
        Settings(tiktok_backend_enabled=True, tiktok_backend_url="https://sidecar.example")
    )
    monkeypatch.setattr(
        backend,
        "_request",
        AsyncMock(return_value={"media_type": media_type, "title": "item", "media": items}),
    )
    monkeypatch.setattr(tiktok_module, "assert_public_dns", lambda url: url)
    monkeypatch.setattr(
        tiktok_module.aiohttp,
        "ClientSession",
        lambda **kwargs: _FakeClientSession(media=media_bytes, **kwargs),
    )
    monkeypatch.setattr(tiktok_module, "probe_media_file", AsyncMock(return_value=None))

    result = await backend.download(
        DownloadRequest(
            "https://www.tiktok.com/@example/video/123",
            tmp_path,
            media_type=media_type,
        )
    )

    assert result.media_type == media_type
    assert [path.name for path in result.files] == expected_names


@pytest.mark.asyncio
async def test_tiktok_sidecar_auth_failure_is_not_a_fallback_error(monkeypatch):
    import app.services.download_backends.tiktok_backend as tiktok_module

    backend = TikTokBackend(
        Settings(tiktok_backend_enabled=True, tiktok_backend_url="https://sidecar.example")
    )
    monkeypatch.setattr(
        tiktok_module.aiohttp,
        "ClientSession",
        lambda **kwargs: _FakeClientSession(post_status=401, **kwargs),
    )

    with pytest.raises(RuntimeError, match="authentication required") as caught:
        await backend._request(
            DownloadRequest(
                "https://www.tiktok.com/@example/video/123",
                Path("."),
            ),
            download=False,
        )

    assert classify_error(caught.value).code is ErrorCode.AUTH_REQUIRED
