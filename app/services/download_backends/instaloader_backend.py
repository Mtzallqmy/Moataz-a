from __future__ import annotations

import asyncio
import importlib.util
import re
from pathlib import Path
from urllib.parse import urlsplit

from app.config import Settings
from app.errors import MediaError
from app.services.download_backends.base import (
    BackendUnavailableError,
    DownloadBackend,
    DownloadRequest,
    NormalizedMediaResult,
)


class InstagramSessionRequiredError(MediaError):
    from app.errors import ErrorCode

    code = ErrorCode.AUTH_REQUIRED


class InstaloaderBackend(DownloadBackend):
    name = "instaloader"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def available(self) -> bool:
        return bool(self.settings.instaloader_enabled and importlib.util.find_spec("instaloader"))

    async def supports(self, url: str, media_type: str | None = None) -> bool:
        host = (urlsplit(url).hostname or "").lower()
        return host.endswith("instagram.com") and media_type in {None, "video", "images", "story", "highlight"}

    @staticmethod
    def _shortcode(url: str) -> str:
        match = re.search(r"/(?:p|reel|reels)/([^/?#]+)", url)
        if not match:
            raise BackendUnavailableError("Instaloader supports Instagram post/reel URLs only for anonymous access")
        return match.group(1)

    @staticmethod
    def _story_media_id(url: str) -> int:
        path = urlsplit(url).path.rstrip("/")
        if "/stories/highlights/" in path.lower() or "/highlights/" in path.lower():
            raise BackendUnavailableError(
                "Single Instagram Highlight item URLs are not safely resolvable by Instaloader"
            )
        match = re.search(r"/(\d+)(?:/|$)", path)
        if not match:
            raise BackendUnavailableError("Instagram Story URL has no media id")
        return int(match.group(1))

    def _session_username(self) -> tuple[str, Path]:
        path = self.settings.instagram_session_file
        if path is None:
            raise InstagramSessionRequiredError("Instagram stories/highlights require INSTAGRAM_SESSION_FILE")
        resolved = path.expanduser().resolve()
        if not resolved.is_file():
            raise InstagramSessionRequiredError("INSTAGRAM_SESSION_FILE does not exist")
        name = resolved.name
        for prefix in ("session-", "instaloader-session-"):
            if name.startswith(prefix):
                name = name[len(prefix):]
        username = name.split(".", 1)[0].strip()
        if not username:
            raise InstagramSessionRequiredError("Instagram session filename must identify its username")
        return username, resolved

    def _loader(self, output_dir: Path):
        import instaloader

        return instaloader.Instaloader(
            dirname_pattern=str(output_dir.resolve()),
            filename_pattern="{shortcode}_{filename}",
            download_pictures=True,
            download_videos=True,
            download_video_thumbnails=False,
            download_geotags=False,
            download_comments=False,
            save_metadata=False,
            compress_json=False,
            post_metadata_txt_pattern="",
            storyitem_metadata_txt_pattern="",
            quiet=True,
        )

    async def probe(self, url: str) -> NormalizedMediaResult:
        if not await self.available():
            raise BackendUnavailableError("Instaloader optional dependency is unavailable")

        path = urlsplit(url).path.lower()
        if "/stories/" in path:
            media_id = self._story_media_id(url)

            def run_story() -> NormalizedMediaResult:
                import instaloader

                loader = self._loader(Path("."))
                username, session_file = self._session_username()
                loader.load_session_from_file(username, str(session_file))
                instaloader.StoryItem.from_mediaid(loader.context, media_id)
                return NormalizedMediaResult(
                    provider=self.name,
                    platform="instagram",
                    media_type="story",
                    title=f"Instagram Story {media_id}",
                    metadata={"media_id": str(media_id)},
                )

            return await asyncio.to_thread(run_story)

        def run_post() -> NormalizedMediaResult:
            import instaloader

            loader = self._loader(Path("."))
            post = instaloader.Post.from_shortcode(loader.context, self._shortcode(url))
            return NormalizedMediaResult(
                provider=self.name,
                platform="instagram",
                media_type="video" if post.is_video else "images",
                title=(post.caption or f"Instagram {post.shortcode}")[:500],
                duration=float(post.video_duration) if post.is_video and post.video_duration else None,
                metadata={"media_id": str(post.mediaid), "shortcode": post.shortcode},
            )

        return await asyncio.to_thread(run_post)

    async def download(self, request: DownloadRequest) -> NormalizedMediaResult:
        if not await self.available():
            raise BackendUnavailableError("Instaloader optional dependency is unavailable")
        request.output_dir.mkdir(parents=True, exist_ok=True)
        before = {p.resolve() for p in request.output_dir.iterdir() if p.is_file()}

        def run() -> None:
            import instaloader

            loader = self._loader(request.output_dir)
            if request.media_type in {"story", "highlight"}:
                username, session_file = self._session_username()
                loader.load_session_from_file(username, str(session_file))
                if request.media_type == "story":
                    media_id = self._story_media_id(request.url)
                    item = instaloader.StoryItem.from_mediaid(loader.context, media_id)
                    loader.download_storyitem(item, request.output_dir)
                    return
                raise BackendUnavailableError(
                    "Single Instagram Highlight item URLs are not safely resolvable by Instaloader"
                )
            post = instaloader.Post.from_shortcode(loader.context, self._shortcode(request.url))
            loader.download_post(post, target=request.output_dir)

        try:
            await asyncio.to_thread(run)
            files = [
                p
                for p in sorted(request.output_dir.rglob("*"))
                if p.is_file()
                and p.resolve() not in before
                and p.suffix.lower() in {".jpg", ".jpeg", ".png", ".mp4", ".webp"}
            ]
            if not files:
                raise RuntimeError("Instaloader produced no media files")
            if sum(p.stat().st_size for p in files) > self.settings.max_file_size_bytes:
                raise RuntimeError("Instaloader outputs exceed configured download limit")
            if request.cancel_event is not None and request.cancel_event.is_set():
                from app.errors import CancelledError

                raise CancelledError("Instaloader download cancelled")
            return NormalizedMediaResult(self.name, "instagram", request.media_type, files=files)
        except Exception:
            for path in request.output_dir.rglob("*"):
                if path.is_file() and path.resolve() not in before:
                    path.unlink(missing_ok=True)
            raise
