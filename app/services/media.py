from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from app.config import get_settings
from app.errors import CancelledError, FFmpegError

settings = get_settings()


@dataclass(frozen=True, slots=True)
class MediaProbe:
    duration: float
    has_video: bool
    has_audio: bool
    size_bytes: int
    width: int | None = None
    height: int | None = None
    format_name: str | None = None


LineCallback = Callable[[str], Awaitable[None] | None]


def target_total_bitrate(limit_bytes: int, duration_seconds: float, margin: float = 0.90) -> int:
    if limit_bytes <= 0 or duration_seconds <= 0:
        raise ValueError("limit_bytes and duration_seconds must be positive")
    safe_margin = max(0.50, min(float(margin), 0.98))
    return max(48_000, int(limit_bytes * 8 * safe_margin / duration_seconds))


def video_height_for_bitrate(total_bitrate: int) -> int:
    if total_bitrate < 550_000:
        return 360
    if total_bitrate < 950_000:
        return 480
    if total_bitrate < 1_900_000:
        return 720
    return 1080


async def _drain(stream: asyncio.StreamReader | None, limit: int) -> bytes:
    if stream is None:
        return b""
    retained = bytearray()
    while True:
        chunk = await stream.read(4096)
        if not chunk:
            break
        retained.extend(chunk)
        if len(retained) > limit:
            del retained[: len(retained) - limit]
    return bytes(retained)


async def _drain_lines(
    stream: asyncio.StreamReader | None,
    limit: int,
    callback: LineCallback,
) -> bytes:
    if stream is None:
        return b""
    retained = bytearray()
    while line := await stream.readline():
        retained.extend(line)
        if len(retained) > limit:
            del retained[: len(retained) - limit]
        result = callback(line.decode("utf-8", errors="replace").rstrip("\r\n"))
        if asyncio.iscoroutine(result):
            await result
    return bytes(retained)


async def _watch_cancel(event: threading.Event | None) -> None:
    if event is None:
        await asyncio.Future()
    while not event.is_set():
        await asyncio.sleep(0.1)


async def _terminate(process: asyncio.subprocess.Process, grace: float) -> None:
    if process.returncode is not None:
        return
    process.terminate()
    try:
        await asyncio.wait_for(process.wait(), timeout=grace)
    except TimeoutError:
        process.kill()
        await process.wait()


async def _run_process(
    *args: str,
    timeout: float | None = None,
    cancel_event: threading.Event | None = None,
    stdout_limit: int = 1_048_576,
    stderr_limit: int | None = None,
    stdout_line_callback: LineCallback | None = None,
) -> tuple[bytes, bytes]:
    process = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out_task = asyncio.create_task(
        _drain_lines(process.stdout, stdout_limit, stdout_line_callback)
        if stdout_line_callback is not None
        else _drain(process.stdout, stdout_limit)
    )
    err_task = asyncio.create_task(_drain(process.stderr, stderr_limit or settings.stderr_limit_bytes))
    wait_task = asyncio.create_task(process.wait())
    cancel_task = asyncio.create_task(_watch_cancel(cancel_event)) if cancel_event is not None else None
    try:
        waiters = {wait_task}
        if cancel_task is not None:
            waiters.add(cancel_task)
        done, _ = await asyncio.wait(waiters, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
        if wait_task not in done:
            await _terminate(process, settings.ffmpeg_kill_grace_seconds)
            if cancel_task is not None and cancel_task in done:
                raise CancelledError("FFmpeg operation cancelled")
            raise FFmpegError("FFmpeg operation timed out")
        code = wait_task.result()
        stdout, stderr = await asyncio.gather(out_task, err_task)
        if code != 0:
            detail = stderr.decode(errors="replace")[-settings.stderr_limit_bytes :]
            raise FFmpegError(f"FFmpeg failed: {detail}")
        return stdout, stderr
    except asyncio.CancelledError:
        await _terminate(process, settings.ffmpeg_kill_grace_seconds)
        raise
    finally:
        tasks = [wait_task, out_task, err_task]
        if cancel_task is not None:
            tasks.append(cancel_task)
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def probe_media_file(source: Path) -> MediaProbe:
    stdout, _ = await _run_process(
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration,format_name:stream=codec_type,width,height",
        "-of",
        "json",
        str(source),
        timeout=60,
    )
    try:
        payload = json.loads(stdout.decode("utf-8"))
        duration = float(payload.get("format", {}).get("duration") or 0)
        streams = payload.get("streams") or []
        stream_types = {stream.get("codec_type") for stream in streams}
        video_stream = next((stream for stream in streams if stream.get("codec_type") == "video"), {})
        width = int(video_stream["width"]) if video_stream.get("width") else None
        height = int(video_stream["height"]) if video_stream.get("height") else None
        format_name = str(payload.get("format", {}).get("format_name") or "") or None
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise FFmpegError("FFprobe returned invalid media metadata") from exc
    if duration <= 0:
        raise FFmpegError("FFprobe could not determine media duration")
    return MediaProbe(
        duration,
        "video" in stream_types,
        "audio" in stream_types,
        source.stat().st_size,
        width,
        height,
        format_name,
    )


def _fast_cut_output(source: Path, probe: MediaProbe) -> Path:
    supported = {".mp4", ".mkv", ".webm", ".mov", ".m4a", ".mp3", ".ogg", ".opus"}
    suffix = source.suffix.lower() if source.suffix.lower() in supported else (".mkv" if probe.has_video else ".m4a")
    return source.with_name(f"{source.stem}.fast{suffix}")


def _precise_cut_output(source: Path, probe: MediaProbe) -> Path:
    suffix = ".mp4" if probe.has_video else ".m4a"
    return source.with_name(f"{source.stem}.precise{suffix}")


def _fast_cut_args(source: Path, output: Path, start: float, duration: float) -> list[str]:
    return [
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
        "-map",
        "0:v:0?",
        "-map",
        "0:a:0?",
        "-sn",
        "-dn",
        "-c",
        "copy",
        "-avoid_negative_ts",
        "make_zero",
        str(output),
    ]


def _precise_cut_args(
    source: Path,
    output: Path,
    start: float,
    duration: float,
    probe: MediaProbe,
    *,
    video_codec: str = "libx264",
) -> list[str]:
    args = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(source),
        "-ss",
        str(start),
        "-t",
        str(duration),
    ]
    if probe.has_video:
        args += [
            "-map",
            "0:v:0",
            "-map",
            "0:a:0?",
            "-sn",
            "-dn",
            "-vf",
            "scale=trunc(iw/2)*2:trunc(ih/2)*2",
            "-c:v",
            video_codec,
            "-pix_fmt",
            "yuv420p",
        ]
        if video_codec == "libx264":
            args += ["-preset", "veryfast", "-crf", "23"]
        else:
            args += ["-q:v", "4"]
        if probe.has_audio:
            args += ["-c:a", "aac", "-b:a", "160k"]
        else:
            args.append("-an")
        args += [
            "-movflags",
            "+faststart",
            "-avoid_negative_ts",
            "make_zero",
            "-max_muxing_queue_size",
            "2048",
            str(output),
        ]
    elif probe.has_audio:
        args += [
            "-map",
            "0:a:0",
            "-vn",
            "-sn",
            "-dn",
            "-c:a",
            "aac",
            "-b:a",
            "160k",
            "-avoid_negative_ts",
            "make_zero",
            str(output),
        ]
    else:
        raise FFmpegError("Downloaded media contains no audio or video stream")
    return args


async def _run_precise_cut(
    source: Path,
    output: Path,
    start: float,
    duration: float,
    probe: MediaProbe,
    *,
    timeout: float,
    cancel_event: threading.Event | None,
) -> None:
    primary = _precise_cut_args(source, output, start, duration, probe, video_codec="libx264")
    try:
        await _run_process(*primary, timeout=timeout, cancel_event=cancel_event)
    except FFmpegError as primary_error:
        if not probe.has_video:
            raise
        output.unlink(missing_ok=True)
        fallback = _precise_cut_args(source, output, start, duration, probe, video_codec="mpeg4")
        try:
            await _run_process(*fallback, timeout=timeout, cancel_event=cancel_event)
        except FFmpegError as fallback_error:
            raise FFmpegError(
                f"Precise cut failed with H.264 and MPEG-4 fallbacks: {fallback_error}"
            ) from primary_error


async def cut_media(
    source: Path,
    start: float,
    end: float,
    *,
    mode: str = "PRECISE",
    source_duration: float | None = None,
    timeout: float | None = None,
    cancel_event: threading.Event | None = None,
) -> Path:
    if start < 0 or end <= start:
        raise ValueError("Invalid clip range")
    if not source.exists() or source.stat().st_size <= 0:
        raise FFmpegError("Downloaded source file is missing or empty")

    probe = await probe_media_file(source)
    actual_duration = probe.duration
    advertised_duration = float(source_duration) if source_duration is not None else actual_duration
    if advertised_duration <= 0:
        advertised_duration = actual_duration
    if start >= actual_duration:
        raise ValueError("Clip start exceeds downloaded media duration")

    requested_end = float(end)
    if requested_end > actual_duration:
        rounding_tolerance = min(2.0, max(0.50, advertised_duration * 0.002))
        if requested_end - actual_duration <= rounding_tolerance:
            requested_end = actual_duration
        else:
            raise ValueError("Clip end exceeds downloaded media duration")

    cut_mode = mode.upper().strip()
    if cut_mode not in {"FAST", "PRECISE"}:
        raise ValueError("Cut mode must be FAST or PRECISE")
    duration = requested_end - float(start)
    if duration <= 0.05:
        raise ValueError("Clip range is too short")

    process_timeout = timeout or settings.ffmpeg_timeout_seconds
    if cut_mode == "FAST":
        output = _fast_cut_output(source, probe)
        try:
            await _run_process(
                *_fast_cut_args(source, output, start, duration),
                timeout=process_timeout,
                cancel_event=cancel_event,
            )
        except FFmpegError:
            output.unlink(missing_ok=True)
            output = _precise_cut_output(source, probe)
            await _run_precise_cut(
                source,
                output,
                start,
                duration,
                probe,
                timeout=process_timeout,
                cancel_event=cancel_event,
            )
    else:
        output = _precise_cut_output(source, probe)
        await _run_precise_cut(
            source,
            output,
            start,
            duration,
            probe,
            timeout=process_timeout,
            cancel_event=cancel_event,
        )

    if not output.exists() or output.stat().st_size <= 0:
        raise FFmpegError("FFmpeg did not produce a valid clip")
    return output


async def _compress_audio(source: Path, output: Path, bitrate: int, cancel_event: threading.Event | None) -> None:
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
        timeout=settings.ffmpeg_timeout_seconds,
        cancel_event=cancel_event,
    )


async def _compress_video(
    source: Path,
    output: Path,
    total_bitrate: int,
    has_audio: bool,
    cancel_event: threading.Event | None,
) -> None:
    audio_bitrate = min(128_000, max(64_000, total_bitrate // 8)) if has_audio else 0
    video_bitrate = max(180_000, total_bitrate - audio_bitrate)
    height = video_height_for_bitrate(total_bitrate)
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
        "-vf",
        f"scale=w=-2:h=min(ih\\,{height})",
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
    if has_audio:
        args += ["-map", "0:a:0?", "-c:a", "aac", "-b:a", str(audio_bitrate)]
    else:
        args.append("-an")
    args += ["-movflags", "+faststart", str(output)]
    await _run_process(*args, timeout=settings.ffmpeg_timeout_seconds, cancel_event=cancel_event)


async def fit_media_for_upload(
    source: Path,
    limit_bytes: int,
    target_dir: Path,
    attempts: int = 2,
    *,
    cancel_event: threading.Event | None = None,
) -> Path:
    if source.stat().st_size <= limit_bytes:
        return source
    if limit_bytes <= 0:
        raise ValueError("limit_bytes must be positive")
    target_dir.mkdir(parents=True, exist_ok=True)
    probe = await probe_media_file(source)
    budget = target_total_bitrate(limit_bytes, probe.duration)
    for attempt in range(1, max(1, min(int(attempts), 4)) + 1):
        if cancel_event is not None and cancel_event.is_set():
            raise CancelledError("Media processing cancelled")
        if probe.has_video:
            output = target_dir / f"{source.stem}.telegram-{attempt}.mp4"
            await _compress_video(source, output, budget, probe.has_audio, cancel_event)
        else:
            output = target_dir / f"{source.stem}.telegram-{attempt}.mp3"
            await _compress_audio(source, output, budget, cancel_event)
        size = output.stat().st_size
        if size <= limit_bytes:
            return output
        budget = max(48_000, int(budget * (limit_bytes / max(size, 1)) * 0.88))
    raise RuntimeError("File exceeds upload limit after adaptive compression")
