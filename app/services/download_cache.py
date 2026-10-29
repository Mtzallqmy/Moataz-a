from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path

from app.errors import FFmpegError
from app.services.media import MediaProbe, probe_media_file


def owned_file(path: str | Path, directory: Path) -> Path:
    """Accept only regular files inside this job's workspace (including symlink checks)."""
    resolved = Path(path).resolve()
    if not resolved.is_relative_to(directory.resolve()) or not resolved.is_file():
        raise ValueError("Saved media is missing or outside the job workspace")
    if resolved.stat().st_size <= 0:
        raise ValueError("Saved media is empty")
    return resolved


async def copy_saved_media(path: str, source_dir: Path, target_dir: Path) -> tuple[Path, MediaProbe]:
    source = owned_file(path, source_dir)
    probe = await probe_media_file(source)
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"saved-source{source.suffix.lower()}"
    # A separate copy lets cleanup expire the original without breaking queued edits.
    await asyncio.to_thread(shutil.copyfile, source, target)
    if target.stat().st_size != probe.size_bytes:
        target.unlink(missing_ok=True)
        raise ValueError("Saved media changed while copying; try again")
    return target, probe


def remember_source(directory: Path, path: Path, quality: str) -> None:
    source = owned_file(path, directory)
    manifest = directory / "download-source.json"
    temporary = directory / "download-source.json.tmp"
    temporary.write_text(json.dumps({"file": str(source.relative_to(directory.resolve())), "quality": quality}))
    temporary.replace(manifest)


async def cached_source(directory: Path, quality: str) -> Path | None:
    manifest = directory / "download-source.json"
    try:
        data = json.loads(manifest.read_text())
        if data["quality"] != quality:
            return None
        source = owned_file(directory / data["file"], directory)
        await probe_media_file(source)
        return source
    except (OSError, ValueError, KeyError, TypeError, FFmpegError):
        return None
