from __future__ import annotations

import threading

import pytest

from app.config import Settings
from app.errors import CancelledError
from app.services.download_relays import CobaltRelayClient


class FakeResponse:
    def __init__(self, *, status=200, payload=None, body=b"media", headers=None):
        self.status_code = status
        self.payload = payload
        self.body = body
        self.headers = headers or {"Content-Length": str(len(body))}

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def json(self):
        return self.payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def iter_content(self, chunk_size):  # noqa: ARG002
        yield self.body


class FakeSession:
    def __init__(self, *, ticket, media=None):
        self.ticket = ticket
        self.media = media or FakeResponse()
        self.posts = []
        self.gets = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def post(self, url, **kwargs):
        self.posts.append((url, kwargs))
        return self.ticket

    def get(self, url, **kwargs):
        self.gets.append((url, kwargs))
        return self.media


def test_cobalt_probe_and_download_are_bounded_and_use_authorization(tmp_path):
    ticket = FakeResponse(
        payload={
            "status": "tunnel",
            "url": "https://cobalt-one.example/tunnel/1",
            "filename": "Example Video.mp4",
        }
    )
    sessions = []

    def factory():
        session = FakeSession(ticket=ticket, media=FakeResponse(body=b"valid-media"))
        sessions.append(session)
        return session

    client = CobaltRelayClient(
        Settings(
            _env_file=None,
            cobalt_api_urls="https://cobalt-one.example",
            cobalt_api_token="relay-secret",
            max_file_size_mb=16,
        ),
        session_factory=factory,
        url_guard=lambda url: url,
    )
    probe = client.probe("https://youtube.com/watch?v=x")
    output = client.download(
        "https://youtube.com/watch?v=x",
        "best",
        tmp_path,
        cancel_event=threading.Event(),
    )
    assert probe.title == "Example Video"
    assert output.read_bytes() == b"valid-media"
    post_headers = sessions[0].posts[0][1]["headers"]
    assert post_headers["Authorization"] == "Api-Key relay-secret"
    assert sessions[-1].gets[0][1]["allow_redirects"] is False
    assert sessions[-1].gets[0][1]["headers"]["Authorization"] == "Api-Key relay-secret"


def test_cobalt_does_not_forward_api_token_to_external_media_host(tmp_path):
    ticket = FakeResponse(
        payload={
            "status": "tunnel",
            "url": "https://cdn.example/media/1",
            "filename": "video.mp4",
        }
    )
    sessions = []

    def factory():
        session = FakeSession(ticket=ticket)
        sessions.append(session)
        return session

    client = CobaltRelayClient(
        Settings(
            _env_file=None,
            cobalt_api_urls="https://cobalt.example",
            cobalt_api_token="relay-secret",
        ),
        session_factory=factory,
        url_guard=lambda url: url,
    )
    client.download(
        "https://youtube.com/watch?v=x",
        "best",
        tmp_path,
        cancel_event=threading.Event(),
    )
    assert "Authorization" not in sessions[-1].gets[0][1]["headers"]


def test_cobalt_rejects_unchecked_redirect_and_removes_partial_output(tmp_path):
    ticket = FakeResponse(
        payload={
            "status": "redirect",
            "url": "https://relay.example/redirect",
            "filename": "video.mp4",
        }
    )
    client = CobaltRelayClient(
        Settings(_env_file=None, cobalt_api_urls="https://cobalt.example"),
        session_factory=lambda: FakeSession(
            ticket=ticket, media=FakeResponse(status=302, headers={"Location": "http://127.0.0.1"})
        ),
        url_guard=lambda url: url,
    )
    with pytest.raises(RuntimeError, match="unchecked redirect"):
        client.download(
            "https://youtube.com/watch?v=x",
            "best",
            tmp_path,
            cancel_event=threading.Event(),
        )
    assert not list(tmp_path.glob("relay-*"))


def test_cobalt_cancellation_removes_partial_output(tmp_path):
    ticket = FakeResponse(
        payload={
            "status": "tunnel",
            "url": "https://relay.example/tunnel",
            "filename": "video.mp4",
        }
    )
    cancelled = threading.Event()
    cancelled.set()
    client = CobaltRelayClient(
        Settings(_env_file=None, cobalt_api_urls="https://cobalt.example"),
        session_factory=lambda: FakeSession(ticket=ticket),
        url_guard=lambda url: url,
    )
    with pytest.raises(CancelledError):
        client.download(
            "https://youtube.com/watch?v=x",
            "best",
            tmp_path,
            cancel_event=cancelled,
        )
    assert not list(tmp_path.glob("relay-*"))


@pytest.mark.parametrize(
    "url",
    ["http://relay.example", "https://", "file:///tmp/relay", "https://ok.example/#fragment"],
)
def test_cobalt_configuration_requires_public_https_roots(url):
    with pytest.raises(ValueError, match="public HTTPS"):
        CobaltRelayClient(Settings(_env_file=None, cobalt_api_urls=url))
