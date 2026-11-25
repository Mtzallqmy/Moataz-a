from __future__ import annotations

import shutil
import time
from contextlib import asynccontextmanager
from urllib.parse import urljoin, urlsplit

import aiohttp

from app.config import Settings
from app.errors import CancelledError, InvalidURLError
from app.security import assert_public_dns
from app.services.artifact_urls import ArtifactTarget, artifact_platform, artifact_target
from app.services.download_backends.base import DownloadBackend, DownloadRequest, NormalizedMediaResult

# Public provider-owned hosts only; never forward credentials or arbitrary URLs.
_REDIRECT_HOSTS = {
    "github": {"github.com", "codeload.github.com", "raw.githubusercontent.com", "release-assets.githubusercontent.com", "objects.githubusercontent.com", "github-releases.githubusercontent.com"},
    "gitlab": {"gitlab.com", "storage.googleapis.com"},
    "huggingface": {"huggingface.co", "cdn-lfs.huggingface.co", "cdn-lfs.hf.co", "cdn-lfs-us-1.hf.co", "cas-bridge.xethub.hf.co"},
    "pypi": {"files.pythonhosted.org"},
}


class ArtifactBackend(DownloadBackend):
    name = "artifact-http"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def supports(self, url: str, media_type: str | None = None) -> bool:
        return artifact_platform(url) is not None

    @asynccontextmanager
    async def _response(self, target: ArtifactTarget, *, timeout: int, method: str = "GET"):
        url = target.url
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout, sock_read=30), trust_env=False, auto_decompress=False, headers={"User-Agent": "MoatazMediaBot/0.6.2", "Accept-Encoding": "identity"}) as session:
            for _ in range(6):
                parsed = urlsplit(url)
                if parsed.scheme != "https" or parsed.hostname not in _REDIRECT_HOSTS[target.platform] or parsed.port not in {None, 443}:
                    raise InvalidURLError("Unsafe software download redirect")
                # All targets must be public, including redirects; no proxy or credentials.
                assert_public_dns(url)
                async with session.request(method, url, allow_redirects=False) as response:
                    if response.status in {301, 302, 303, 307, 308}:
                        location = response.headers.get("Location")
                        if not location:
                            raise InvalidURLError("Download redirect missing destination")
                        url = urljoin(url, location)
                        continue
                    if response.status >= 400:
                        if response.status == 404:
                            raise RuntimeError("Media unavailable: file or repository not found or not public")
                        raise RuntimeError(f"HTTP {response.status}: software provider rejected the request")
                    if response.status != 200:
                        raise RuntimeError(f"Unexpected HTTP {response.status} from software provider")
                    # Archive/download pages may not be mistaken for binary files.
                    if target.filename.endswith((".zip", ".tar.gz", ".apk", ".whl")) and response.content_type == "text/html":
                        raise RuntimeError("Media unavailable: provider returned a web page instead of a file")
                    yield response
                    return
            raise InvalidURLError("Too many software download redirects")

    def _result(self, target: ArtifactTarget, size: int | None, files=None) -> NormalizedMediaResult:
        return NormalizedMediaResult(self.name, target.platform, "document", title=target.filename,
                                     files=files or [], metadata={"file_size": size, "webpage_url": target.url})

    async def probe(self, url: str) -> NormalizedMediaResult:
        target = artifact_target(url)
        # GET headers are reliable even for providers that do not implement HEAD.
        # Closing the response immediately avoids downloading the body at analysis time.
        async with self._response(target, timeout=self.settings.probe_timeout_seconds) as response:
            return self._result(target, response.content_length)

    async def download(self, request: DownloadRequest) -> NormalizedMediaResult:
        if request.quality != "best":
            raise ValueError("Software files can only be downloaded in original format")
        target = artifact_target(request.url)
        # Keep user-named files separate from the job cache manifest.
        path = request.output_dir / "artifact" / target.filename
        partial = path.with_name(path.name + ".part")
        limit = min(self.settings.artifact_download_limit_bytes, request.max_bytes) if request.max_bytes is not None else self.settings.artifact_download_limit_bytes
        start = time.monotonic()
        count = 0
        try:
            if request.cancel_event and request.cancel_event.is_set():
                raise CancelledError("Download cancelled")
            async with self._response(target, timeout=self.settings.download_timeout_seconds) as response:
                total = response.content_length
                if total is not None and total > limit:
                    raise RuntimeError("File too large: exceeds artifact download limit")
                path.parent.mkdir(parents=True, exist_ok=True)
                # Keep room for the database/other jobs on the shared volume.
                reserve = 16 * 1024 * 1024
                if shutil.disk_usage(path.parent).free < reserve + (total or 0):
                    raise OSError("No space left for software file")
                with partial.open("wb") as stream:
                    async for chunk in response.content.iter_chunked(128 * 1024):
                        if request.cancel_event and request.cancel_event.is_set():
                            raise CancelledError("Download cancelled")
                        count += len(chunk)
                        if count > limit:
                            raise RuntimeError("File too large: exceeds artifact download limit")
                        if shutil.disk_usage(path.parent).free < reserve + len(chunk):
                            raise OSError("No space left for software file")
                        stream.write(chunk)
                        if request.progress_hook:
                            speed = count / max(0.001, time.monotonic() - start)
                            request.progress_hook({"status": "downloading", "downloaded_bytes": count, "total_bytes": total or 0, "speed": speed, "eta": (total - count) / speed if total else None})
                if not count or (total is not None and count != total):
                    raise RuntimeError("Incomplete software download")
                if request.cancel_event and request.cancel_event.is_set():
                    raise CancelledError("Download cancelled")
                partial.replace(path)
            return self._result(target, count, [path])
        finally:
            partial.unlink(missing_ok=True)
            if path.parent.exists() and not any(path.parent.iterdir()):
                path.parent.rmdir()
