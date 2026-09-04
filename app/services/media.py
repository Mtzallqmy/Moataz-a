from __future__ import annotations

import asyncio
from pathlib import Path


async def cut_media(source: Path, start: float, end: float) -> Path:
    if start < 0 or end <= start:
        raise ValueError("Invalid clip range")
    output = source.with_name(f"{source.stem}.clip.mp4")
    duration = end - start
    process = await asyncio.create_subprocess_exec(
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-ss",
        str(start),
        "-i",
        str(source),
        "-t",
        str(duration),
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "23",
        "-c:a",
        "aac",
        "-b:a",
        "160k",
        "-movflags",
        "+faststart",
        str(output),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await process.communicate()
    if process.returncode != 0:
        raise RuntimeError(f"FFmpeg failed: {stderr.decode(errors='ignore')[-1000:]}")
    return output
