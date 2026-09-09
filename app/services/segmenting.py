from __future__ import annotations

import math
import shutil
import threading
from pathlib import Path

from app.config import get_settings
from app.errors import CancelledError, FFmpegError
from app.services.media import MediaProbe, _run_process, probe_media_file

settings = get_settings()
MAX_SPLIT_SEGMENTS = 80


def _normalize_range(
    probe: MediaProbe,
    *,
    start: float,
    end: float | None,
    segment_seconds: int,
) -> tuple[float, float, int]:
    if segment_seconds not in {30, 60}:
        raise ValueError("Segment length must be 30 or 60 seconds")
    start_value = float(start)
    if start_value < 0 or start_value >= probe.duration:
        raise ValueError("Split start is outside the media duration")
    end_value = probe.duration if end is None else float(end)
    if end_value > probe.duration:
        tolerance = min(2.0, max(0.5, probe.duration * 0.002))
        if end_value - probe.duration <= tolerance:
            end_value = probe.duration
        else:
            raise ValueError("Split end exceeds media duration")
    if end_value <= start_value:
        raise ValueError("Split end must be after the start")
    count = int(math.ceil((end_value - start_value) / segment_seconds))
    if count > MAX_SPLIT_SEGMENTS:
        raise ValueError(
            f"Split would create {count} files; limit is {MAX_SPLIT_SEGMENTS}. "
            "Use 60-second segments or choose a shorter range."
        )
    return start_value, end_value, count


def _video_segment_args(
    source: Path,
    pattern: Path,
    *,
    start: float,
    duration: float,
    segment_seconds: int,
    probe: MediaProbe,
    codec: str,
) -> list[str]:
    args = [
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
        "0:v:0",
        "-map",
        "0:a:0?",
        "-sn",
        "-dn",
        "-vf",
        "scale=trunc(iw/2)*2:trunc(ih/2)*2",
        "-c:v",
        codec,
        "-pix_fmt",
        "yuv420p",
    ]
    if codec == "libx264":
        args += ["-preset", "veryfast", "-crf", "23"]
    else:
        args += ["-q:v", "4"]
    args += [
        "-force_key_frames",
        f"expr:gte(t,n_forced*{segment_seconds})",
    ]
    if probe.has_audio:
        args += ["-c:a", "aac", "-b:a", "160k"]
    else:
        args.append("-an")
    args += [
        "-f",
        "segment",
        "-segment_time",
        str(segment_seconds),
        "-reset_timestamps",
        "1",
        "-segment_format",
        "mp4",
        "-segment_format_options",
        "movflags=+faststart",
        str(pattern),
    ]
    return args


def _audio_segment_args(
    source: Path,
    pattern: Path,
    *,
    start: float,
    duration: float,
    segment_seconds: int,
) -> list[str]:
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
        "0:a:0",
        "-vn",
        "-sn",
        "-dn",
        "-c:a",
        "libmp3lame",
        "-b:a",
        "192k",
        "-f",
        "segment",
        "-segment_time",
        str(segment_seconds),
        "-reset_timestamps",
        "1",
        str(pattern),
    ]


async def split_media(
    source: Path,
    segment_seconds: int,
    *,
    start: float = 0.0,
    end: float | None = None,
    timeout: float | None = None,
    cancel_event: threading.Event | None = None,
) -> list[Path]:
    """Split one downloaded media file into consecutive 30s/60s parts in one FFmpeg pass."""
    if not source.exists() or source.stat().st_size <= 0:
        raise FFmpegError("Downloaded source file is missing or empty")
    if cancel_event is not None and cancel_event.is_set():
        raise CancelledError("Media segmentation cancelled")

    probe = await probe_media_file(source)
    start_value, end_value, expected_count = _normalize_range(
        probe,
        start=start,
        end=end,
        segment_seconds=segment_seconds,
    )
    duration = end_value - start_value
    output_dir = source.parent / f"segments-{segment_seconds}-{int(start_value)}"
    shutil.rmtree(output_dir, ignore_errors=True)
    output_dir.mkdir(parents=True, exist_ok=True)
    process_timeout = timeout or settings.ffmpeg_timeout_seconds

    if probe.has_video:
        pattern = output_dir / "part-%03d.mp4"
        primary = _video_segment_args(
            source,
            pattern,
            start=start_value,
            duration=duration,
            segment_seconds=segment_seconds,
            probe=probe,
            codec="libx264",
        )
        try:
            await _run_process(*primary, timeout=process_timeout, cancel_event=cancel_event)
        except FFmpegError as primary_error:
            for item in output_dir.glob("part-*.mp4"):
                item.unlink(missing_ok=True)
            fallback = _video_segment_args(
                source,
                pattern,
                start=start_value,
                duration=duration,
                segment_seconds=segment_seconds,
                probe=probe,
                codec="mpeg4",
            )
            try:
                await _run_process(*fallback, timeout=process_timeout, cancel_event=cancel_event)
            except FFmpegError as fallback_error:
                raise FFmpegError(f"Video segmentation failed: {fallback_error}") from primary_error
        parts = sorted(output_dir.glob("part-*.mp4"))
    elif probe.has_audio:
        pattern = output_dir / "part-%03d.mp3"
        await _run_process(
            *_audio_segment_args(
                source,
                pattern,
                start=start_value,
                duration=duration,
                segment_seconds=segment_seconds,
            ),
            timeout=process_timeout,
            cancel_event=cancel_event,
        )
        parts = sorted(output_dir.glob("part-*.mp3"))
    else:
        raise FFmpegError("Downloaded media contains no audio or video stream")

    if not parts:
        raise FFmpegError("FFmpeg segmentation produced no media parts")
    if len(parts) > MAX_SPLIT_SEGMENTS:
        raise FFmpegError("FFmpeg segmentation exceeded the safe segment limit")
    if any(not part.exists() or part.stat().st_size <= 0 for part in parts):
        raise FFmpegError("FFmpeg segmentation produced an empty part")
    if len(parts) > expected_count + 1:
        raise FFmpegError("FFmpeg segmentation produced an unexpected number of parts")
    return parts
