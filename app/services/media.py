from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class MediaProbe:
    duration: float
    has_video: bool
    has_audio: bool
    size_bytes: int


def target_total_bitrate(limit_bytes: int, duration_seconds: float, margin: float = 0.90) -> int:
    """Return a conservative total bitrate budget in bits/s for a target file size."""

    if limit_bytes <= 0 or duration_seconds <= 0:
        raise ValueError("limit_bytes and duration_seconds must be positive")
    safe_margin = max(0.50, min(float(margin), 0.98))
    return max(48_000, int(limit_bytes * 8 * safe_margin / duration_seconds))


def video_height_for_bitrate(total_bitrate: int) -> int:
    """Choose a conservative height ceiling for constrained Telegram uploads."""

    if total_bitrate < 550_000:
        return 360
    if total_bitrate < 950_000:
        return 480
    if total_bitrate < 1_900_000:
        return 720
    return 1080


async def _run_process(*args: str) -> tuple[bytes, bytes]:
    process = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate()
    if process.returncode != 0:
        detail = stderr.decode(errors="ignore")[-1600:]
        raise RuntimeError(f"FFmpeg failed: {detail}")
    return stdout, stderr


async def probe_media_file(source: Path) -> MediaProbe:
    stdout, _ = await _run_process(
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration:stream=codec_type",
        "-of",
        "json",
        str(source),
    )
    try:
        payload = json.loads(stdout.decode("utf-8"))
        duration = float(payload.get("format", {}).get("duration") or 0)
        stream_types = {stream.get("codec_type") for stream in payload.get("streams") or []}
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError("FFprobe returned invalid media metadata") from exc
    if duration <= 0:
        raise RuntimeError("FFprobe could not determine media duration")
    return MediaProbe(
        duration=duration,
        has_video="video" in stream_types,
        has_audio="audio" in stream_types,
        size_bytes=source.stat().st_size,
    )


async def cut_media(source: Path, start: float, end: float) -> Path:
    if start < 0 or end <= start:
        raise ValueError("Invalid clip range")
    output = source.with_name(f"{source.stem}.clip.mp4")
    duration = end - start
    await _run_process(
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
    )
    return output


async def _compress_audio(source: Path, output: Path, bitrate: int) -> None:
    bitrate_k = max(32, min(192, bitrate // 1000))
    await _run_process(
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(source),
        "-vn",
        "-c:a",
        "libmp3lame",
        "-b:a",
        f"{bitrate_k}k",
        str(output),
    )


async def _compress_video(
    source: Path,
    output: Path,
    total_bitrate: int,
    has_audio: bool,
) -> None:
    audio_bitrate = min(128_000, max(64_000, total_bitrate // 8)) if has_audio else 0
    video_bitrate = max(180_000, total_bitrate - audio_bitrate)
    height = video_height_for_bitrate(total_bitrate)
    scale_filter = f"scale=w=-2:h=min(ih\\,{height})"
    args = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(source),
        "-map",
        "0:v:0",
    ]
    if has_audio:
        args.extend(["-map", "0:a:0?"])
    args.extend(
        [
            "-vf",
            scale_filter,
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-b:v",
            str(video_bitrate),
            "-maxrate",
            str(int(video_bitrate * 1.15)),
            "-bufsize",
            str(video_bitrate * 2),
        ]
    )
    if has_audio:
        args.extend(["-c:a", "aac", "-b:a", str(audio_bitrate)])
    else:
        args.append("-an")
    args.extend(["-movflags", "+faststart", str(output)])
    await _run_process(*args)


async def fit_media_for_upload(
    source: Path,
    limit_bytes: int,
    target_dir: Path,
    attempts: int = 2,
) -> Path:
    """Adaptively transcode media that is larger than the configured Telegram upload budget."""

    if source.stat().st_size <= limit_bytes:
        return source
    if limit_bytes <= 0:
        raise ValueError("limit_bytes must be positive")

    target_dir.mkdir(parents=True, exist_ok=True)
    probe = await probe_media_file(source)
    budget = target_total_bitrate(limit_bytes, probe.duration)
    max_attempts = max(1, min(int(attempts), 4))

    for attempt in range(1, max_attempts + 1):
        if probe.has_video:
            output = target_dir / f"{source.stem}.telegram-{attempt}.mp4"
            await _compress_video(source, output, budget, probe.has_audio)
        else:
            output = target_dir / f"{source.stem}.telegram-{attempt}.mp3"
            await _compress_audio(source, output, budget)

        output_size = output.stat().st_size
        if output_size <= limit_bytes:
            return output

        ratio = limit_bytes / max(output_size, 1)
        budget = max(48_000, int(budget * ratio * 0.88))

    raise RuntimeError("File exceeds upload limit after adaptive compression")
