from __future__ import annotations

import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.config import Settings
from app.services.download_backends.instaloader_backend import InstaloaderBackend


@pytest.mark.asyncio
async def test_instaloader_story_probe_uses_session_and_single_media_id(monkeypatch, tmp_path):
    session_file = tmp_path / "session-storyuser"
    session_file.write_text("opaque-session", encoding="utf-8")
    loaded: list[tuple[str, str]] = []
    resolved_ids: list[int] = []

    class FakeLoader:
        context = object()

        def load_session_from_file(self, username: str, path: str) -> None:
            loaded.append((username, path))

    class FakeStoryItem:
        @staticmethod
        def from_mediaid(_context, media_id: int):
            resolved_ids.append(media_id)
            return SimpleNamespace(media_id=media_id)

    monkeypatch.setitem(
        sys.modules,
        "instaloader",
        SimpleNamespace(
            Instaloader=lambda **_kwargs: FakeLoader(),
            StoryItem=FakeStoryItem,
        ),
    )
    backend = InstaloaderBackend(
        Settings(instaloader_enabled=True, instagram_session_file=session_file)
    )
    monkeypatch.setattr(backend, "available", AsyncMock(return_value=True))

    result = await backend.probe(
        "https://www.instagram.com/stories/example/987654321/"
    )

    assert result.media_type == "story"
    assert result.metadata == {"media_id": "987654321"}
    assert loaded == [("storyuser", str(session_file.resolve()))]
    assert resolved_ids == [987654321]
