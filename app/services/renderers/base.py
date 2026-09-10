from __future__ import annotations

import inspect
import threading
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from app.services.composer import RenderPlan

ProgressCallback = Callable[[float], Awaitable[None] | None]


@dataclass(frozen=True, slots=True)
class RenderResult:
    output_path: Path
    duration: float
    file_size: int
    has_audio: bool


async def emit_progress(callback: ProgressCallback | None, value: float) -> None:
    if callback is None:
        return
    result = callback(max(0.0, min(float(value), 1.0)))
    if inspect.isawaitable(result):
        await result


class BaseRenderer:
    async def render(
        self,
        plan: RenderPlan,
        *,
        render_job_id: int,
        progress_callback: ProgressCallback | None = None,
        cancel_event: threading.Event | None = None,
    ) -> RenderResult:
        raise NotImplementedError
