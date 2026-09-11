from __future__ import annotations

import asyncio

from app.services.download_backends.base import DownloadBackend, DownloadRequest, NormalizedMediaResult
from app.services.download_relays import CobaltRelayClient


class CobaltBackend(DownloadBackend):
    name = "cobalt"

    def __init__(self, relay: CobaltRelayClient) -> None:
        self.relay = relay

    async def available(self) -> bool:
        return self.relay.enabled

    async def supports(self, url: str, media_type: str | None = None) -> bool:  # noqa: ARG002
        return media_type in {None, "video", "audio", "playlist"}

    async def probe(self, url: str) -> NormalizedMediaResult:
        result = await asyncio.to_thread(self.relay.probe, url)
        return NormalizedMediaResult(
            provider=self.name,
            platform=result.platform,
            media_type="video",
            title=result.title,
            metadata={"endpoint_index": result.endpoint_index, "extractor": f"cobalt-{result.endpoint_index}"},
        )

    async def download(self, request: DownloadRequest) -> NormalizedMediaResult:
        if request.cancel_event is None:
            import threading

            cancel_event = threading.Event()
        else:
            cancel_event = request.cancel_event
        output = await asyncio.to_thread(
            self.relay.download,
            request.url,
            request.quality,
            request.output_dir,
            cancel_event=cancel_event,
            progress_hook=request.progress_hook,
        )
        return NormalizedMediaResult(
            provider=self.name,
            platform="generic",
            media_type=request.media_type,
            files=[output],
        )
