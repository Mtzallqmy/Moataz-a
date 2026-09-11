import base64
from pathlib import Path

import pytest

pytest.importorskip("yt_dlp")

from app.config import Settings
from app.errors import CancelledError, ErrorCode, FormatUnavailableError
from app.services.download_relays import RelayProbe
from app.services.downloader import DownloaderService


class FakeYDL:
    info = {}
    last_options = None
    download_callback = None

    def __init__(self, options):
        type(self).last_options = options
        self.options = options

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def extract_info(self, url, download=False):  # noqa: ARG002
        return type(self).info

    def download(self, urls):  # noqa: ARG002
        if type(self).download_callback:
            type(self).download_callback(self.options)
        return 0


def service(**kwargs):
    return DownloaderService(
        Settings(_env_file=None, max_playlist_items=3),
        ydl_factory=FakeYDL,
        url_guard=lambda url: url,
        **kwargs,
    )


def test_probe_extracts_media_info_and_actual_formats():
    FakeYDL.info = {
        "id": "x1",
        "title": "A video",
        "duration": 42,
        "thumbnail": "https://img.example/x.jpg",
        "uploader": "Creator",
        "extractor_key": "Youtube",
        "webpage_url": "https://youtu.be/x1",
        "formats": [
            {"format_id": "18", "height": 360, "vcodec": "avc1", "ext": "mp4", "tbr": 500},
            {"format_id": "22", "height": 720, "vcodec": "avc1", "ext": "mp4", "tbr": 1400},
        ],
    }
    info = service().probe("https://youtu.be/x1")
    assert info.title == "A video"
    assert info.duration == 42
    assert info.uploader == "Creator"
    assert info.platform == "youtube"
    assert info.qualities == [360, 720]
    assert [f.format_id for f in info.formats] == ["18", "22"]


def test_generic_extractor_is_preserved_only_when_ytdlp_recognizes_it():
    FakeYDL.info = {
        "id": "x",
        "title": "Generic Site Video",
        "extractor_key": "Vimeo",
        "formats": [{"format_id": "1", "height": 720, "vcodec": "h264"}],
    }
    assert service().probe("https://vimeo.com/1").platform == "vimeo"


def test_playlist_probe_reports_count_without_downloading():
    FakeYDL.info = {
        "_type": "playlist",
        "title": "List",
        "extractor_key": "YoutubeTab",
        "entries": [{"url": "a"}, {"url": "b"}, {"url": "c"}],
    }
    info = service().probe("https://youtube.com/playlist?list=x")
    assert info.is_playlist is True
    assert info.playlist_count == 3
    assert FakeYDL.last_options["skip_download"] is True
    assert FakeYDL.last_options["playlistend"] == 4


def test_playlist_expansion_deduplicates_and_enforces_limit():
    FakeYDL.info = {
        "_type": "playlist",
        "entries": [
            {"webpage_url": "https://example.com/1", "title": "1"},
            {"webpage_url": "https://example.com/1#x", "title": "dup"},
            {"webpage_url": "https://example.com/2", "title": "2"},
        ],
    }
    entries = service().expand_playlist("https://example.com/list", limit=3)
    assert [entry.url for entry in entries] == ["https://example.com/1", "https://example.com/2"]

    FakeYDL.info = {
        "_type": "playlist",
        "entries": [{"webpage_url": f"https://example.com/{i}"} for i in range(4)],
    }
    with pytest.raises(ValueError, match="safe limit"):
        service().expand_playlist("https://example.com/list", limit=3)


def test_playlist_expansion_uses_configured_proxy_after_antibot():
    class PlaylistFailoverYDL(FakeYDL):
        options_seen = []

        def __init__(self, options):
            super().__init__(options)
            type(self).options_seen.append(options)

        def extract_info(self, url, download=False):  # noqa: ARG002
            if "proxy" not in self.options:
                raise RuntimeError("Sign in to confirm you’re not a bot")
            return {
                "_type": "playlist",
                "entries": [{"webpage_url": "https://example.com/one"}],
            }

    configured = DownloaderService(
        Settings(
            _env_file=None,
            max_playlist_items=3,
            ytdlp_proxy_urls="https://proxy.example:8443",
        ),
        ydl_factory=PlaylistFailoverYDL,
        url_guard=lambda url: url,
    )
    entries = configured.expand_playlist("https://example.com/list")
    assert [entry.url for entry in entries] == ["https://example.com/one"]
    assert len(PlaylistFailoverYDL.options_seen) == 2
    assert PlaylistFailoverYDL.options_seen[-1]["proxy"] == "https://proxy.example:8443"


def test_requested_missing_resolution_fails_before_ytdlp_download(tmp_path):
    with pytest.raises(FormatUnavailableError):
        service().download(
            "https://example.com/v",
            "1080",
            tmp_path,
            job_key="1",
            known_qualities=[360, 720],
        )


def test_mp3_download_configures_ffmpeg_extract_audio(tmp_path):
    def create_audio(options):
        output = tmp_path / "audio.mp3"
        output.write_bytes(b"mp3")
        hook = options["progress_hooks"][0]
        hook({"status": "downloading", "downloaded_bytes": 1, "total_bytes": 2})

    FakeYDL.download_callback = create_audio
    result = service().download_audio("https://example.com/v", tmp_path, job_key="audio")
    assert result.suffix == ".mp3"
    post = FakeYDL.last_options["postprocessors"][0]
    assert post["key"] == "FFmpegExtractAudio"
    assert post["preferredcodec"] == "mp3"
    FakeYDL.download_callback = None


def test_download_cancellation_is_observed_by_progress_hook(tmp_path):
    svc = service()

    def cancel_inside(options):
        svc.cancel("j")
        options["progress_hooks"][0]({"status": "downloading"})

    FakeYDL.download_callback = cancel_inside
    with pytest.raises(CancelledError):
        svc.download("https://example.com/v", "best", tmp_path, job_key="j")
    FakeYDL.download_callback = None


def test_downloader_error_classifier_uses_unified_codes():
    info = service().classify_error(RuntimeError("HTTP Error 503 Service Unavailable"))
    assert info.code is ErrorCode.UPSTREAM_5XX
    assert info.retryable is True


def test_probe_passes_optional_cookie_file_to_ytdlp(tmp_path):
    cookies = tmp_path / "cookies.txt"
    cookies.write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")
    FakeYDL.info = {"id": "x", "title": "x", "formats": []}
    configured = DownloaderService(
        Settings(_env_file=None, ytdlp_cookies_file=cookies),
        ydl_factory=FakeYDL,
        url_guard=lambda url: url,
    )
    configured.probe("https://example.com/x")
    assert FakeYDL.last_options["cookiefile"] == str(cookies.resolve())


def test_base64_cookies_are_materialized_privately(tmp_path):
    content = b"# Netscape HTTP Cookie File\n.example.com\tTRUE\t/\tTRUE\t0\tsid\tsecret\n"
    configured = DownloaderService(
        Settings(
            _env_file=None,
            render_temp_dir=tmp_path,
            ytdlp_cookies_b64=base64.b64encode(content).decode(),
        ),
        ydl_factory=FakeYDL,
        url_guard=lambda url: url,
    )
    options = configured._base_options()
    cookiefile = Path(options["cookiefile"])
    assert cookiefile.read_bytes() == content
    assert cookiefile.stat().st_mode & 0o077 == 0


def test_invalid_cookie_secret_fails_before_network(tmp_path):
    configured = DownloaderService(
        Settings(_env_file=None, render_temp_dir=tmp_path, ytdlp_cookies_b64="not-base64"),
        ydl_factory=FakeYDL,
        url_guard=lambda url: url,
    )
    with pytest.raises(ValueError, match="valid base64"):
        configured.probe("https://example.com/x")


class FakeRelay:
    enabled = True

    def __init__(self) -> None:
        self.probes = []
        self.downloads = []

    def probe(self, url):
        self.probes.append(url)
        return RelayProbe("Relay title", "youtube", 1)

    def download(self, url, quality, job_dir, **kwargs):
        self.downloads.append((url, quality))
        output = job_dir / ("relay.mp3" if quality in {"audio", "mp3"} else "relay.mp4")
        output.write_bytes(b"relay-media")
        return output


class AntiBotYDL(FakeYDL):
    def extract_info(self, url, download=False):  # noqa: ARG002
        raise RuntimeError("Sign in to confirm you’re not a bot")

    def download(self, urls):  # noqa: ARG002
        raise RuntimeError("Sign in to confirm you’re not a bot")


def test_probe_falls_back_to_configured_relay_after_antibot():
    relay = FakeRelay()
    configured = DownloaderService(
        Settings(_env_file=None),
        ydl_factory=AntiBotYDL,
        url_guard=lambda url: url,
        relay=relay,  # type: ignore[arg-type]
    )
    info = configured.probe("https://youtube.com/watch?v=x")
    assert info.title == "Relay title"
    assert info.extractor == "cobalt-1"
    assert info.qualities == []
    assert relay.probes == ["https://youtube.com/watch?v=x"]


def test_download_falls_back_to_relay_for_video_and_audio(tmp_path):
    relay = FakeRelay()
    configured = DownloaderService(
        Settings(_env_file=None),
        ydl_factory=AntiBotYDL,
        url_guard=lambda url: url,
        relay=relay,  # type: ignore[arg-type]
    )
    video = configured.download(
        "https://youtube.com/watch?v=x", "best", tmp_path / "video", job_key="v"
    )
    audio = configured.download_audio(
        "https://youtube.com/watch?v=x", tmp_path / "audio", job_key="a"
    )
    assert video.suffix == ".mp4"
    assert audio.suffix == ".mp3"
    assert relay.downloads == [
        ("https://youtube.com/watch?v=x", "best"),
        ("https://youtube.com/watch?v=x", "audio"),
    ]


def test_proxy_failover_profiles_are_ordered(monkeypatch):
    monkeypatch.setitem(__import__("sys").modules, "curl_cffi", object())
    configured = DownloaderService(
        Settings(
            _env_file=None,
            ytdlp_proxy_urls="http://user:secret@proxy-one.example:8080,"
            "socks5h://proxy-two.example:1080",
        ),
        url_guard=lambda url: url,
    )
    attempts = configured._attempts()
    assert [attempt.name for attempt in attempts] == [
        "direct",
        "browser",
        "proxy-1",
        "proxy-2",
    ]
    assert attempts[2].options["proxy"].startswith("http://user:secret@")
