from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

from app.config import Settings
from app.errors import CancelledError
from app.services.download_backends.base import BackendUnavailableError, DownloadBackend, DownloadRequest, NormalizedMediaResult
from app.services.media import probe_media_file

_IMAGE_MAGIC = (b"\xff\xd8\xff", b"\x89PNG\r\n\x1a\n", b"RIFF", b"GIF87a", b"GIF89a")
_PARTIAL_SUFFIXES = {".part", ".tmp", ".temp", ".download"}


class GalleryDlBackend(DownloadBackend):
    """GPL-separated gallery-dl CLI adapter. No gallery-dl code is imported."""

    name = "gallery-dl"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def available(self) -> bool:
        return bool(self.settings.gallerydl_enabled and shutil.which("gallery-dl"))

    async def supports(self, url: str, media_type: str | None = None) -> bool:  # noqa: ARG002
        return media_type in {None, "video", "audio", "images", "story", "highlight"}

    @staticmethod
    async def _terminate(process: asyncio.subprocess.Process, grace: float = 3.0) -> None:
        if process.returncode is not None:
            return
        process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=grace)
        except TimeoutError:
            process.kill()
            await process.wait()

    async def _validate(self, path: Path) -> bool:
        if not path.is_file() or path.stat().st_size <= 0:
            return False
        if path.stat().st_size > self.settings.max_file_size_bytes:
            return False
        head = path.read_bytes()[:16]
        if any(head.startswith(magic) for magic in _IMAGE_MAGIC):
            return True
        try:
            await probe_media_file(path)
        except Exception:
            return False
        return True

    async def download(self, request: DownloadRequest) -> NormalizedMediaResult:
        executable = shutil.which("gallery-dl")
        if not self.settings.gallerydl_enabled or not executable:
            raise BackendUnavailableError("gallery-dl executable is not installed or backend is disabled")
        request.output_dir.mkdir(parents=True, exist_ok=True)
        before = {path.resolve() for path in request.output_dir.iterdir() if path.is_file()}
        args = [
            executable,
            "--config-ignore",
            "--no-input",
            "--no-colors",
            "--directory",
            str(request.output_dir.resolve()),
            "--restrict-filenames",
            "unix",
            "--filesize-max",
            str(self.settings.max_file_size_bytes),
        ]
        cookie_file = self.settings.gallerydl_cookie_file
        if cookie_file is not None:
            cookie_path = cookie_file.expanduser().resolve()
            if not cookie_path.is_file():
                raise BackendUnavailableError("GALLERYDL_COOKIE_FILE does not exist")
            args += ["--cookies", str(cookie_path)]
        args.append(request.url)
        process = await asyncio.create_subprocess_exec(
            *args,
            cwd=request.output_dir,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        wait_task = asyncio.create_task(process.communicate())
        cancel_task: asyncio.Task[None] | None = None
        if request.cancel_event is not None:
            async def watch_cancel() -> None:
                while not request.cancel_event.is_set():
                    await asyncio.sleep(0.1)
            cancel_task = asyncio.create_task(watch_cancel())
        try:
            waiters = {wait_task}
            if cancel_task is not None:
                waiters.add(cancel_task)
            done, _ = await asyncio.wait(
                waiters,
                timeout=self.settings.gallerydl_timeout_seconds,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if wait_task not in done:
                await self._terminate(process)
                if cancel_task is not None and cancel_task in done:
                    raise CancelledError("gallery-dl download cancelled")
                raise TimeoutError("gallery-dl download timed out")
            stdout, stderr = await wait_task
            if process.returncode:
                detail = stderr.decode("utf-8", errors="replace")[-2000:]
                raise RuntimeError(f"gallery-dl failed with exit code {process.returncode}: {detail}")
            files: list[Path] = []
            total = 0
            for path in sorted(request.output_dir.rglob("*")):
                if not path.is_file() or path.resolve() in before or path.suffix.lower() in _PARTIAL_SUFFIXES:
                    continue
                if not path.resolve().is_relative_to(request.output_dir.resolve()):
                    continue
                if await self._validate(path):
                    total += path.stat().st_size
                    if total > self.settings.max_file_size_bytes:
                        raise RuntimeError("gallery-dl outputs exceed configured download limit")
                    files.append(path)
            if not files:
                detail = stdout.decode("utf-8", errors="replace")[-1000:]
                raise RuntimeError(f"gallery-dl produced no validated media files: {detail}")
            return NormalizedMediaResult(
                provider=self.name,
                platform="generic",
                media_type=request.media_type,
                files=files,
            )
        except Exception:
            for path in request.output_dir.rglob("*"):
                if path.is_file() and path.resolve() not in before:
                    path.unlink(missing_ok=True)
            raise
        finally:
            if cancel_task is not None and not cancel_task.done():
                cancel_task.cancel()
            if cancel_task is not None:
                await asyncio.gather(cancel_task, return_exceptions=True)
