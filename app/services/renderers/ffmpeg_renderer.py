from __future__ import annotations

import asyncio
import shutil
import threading
from pathlib import Path

from app.config import Settings, get_settings
from app.errors import CancelledError, FFmpegError
from app.services.composer import RenderAsset, RenderPlan
from app.services.media import _run_process, probe_media_file
from app.services.renderers.base import BaseRenderer, ProgressCallback, RenderResult, emit_progress


class FFmpegRenderer(BaseRenderer):
    """FFmpeg-first renderer for the Media Studio MVP.

    The renderer consumes only RenderPlan/RenderAsset values. It has no Telegram,
    SQLAlchemy, URL-download, or AI dependencies.
    """

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    async def render(
        self,
        plan: RenderPlan,
        *,
        render_job_id: int,
        progress_callback: ProgressCallback | None = None,
        cancel_event: threading.Event | None = None,
    ) -> RenderResult:
        self._check_cancel(cancel_event)
        workspace = self.settings.render_temp_dir / f"render-{int(render_job_id)}"
        output_dir = self.settings.project_dir / str(plan.project_id) / "renders" / str(int(render_job_id))
        shutil.rmtree(workspace, ignore_errors=True)
        workspace.mkdir(parents=True, exist_ok=False)
        output_dir.mkdir(parents=True, exist_ok=True)
        output = output_dir / "output.mp4"
        output.unlink(missing_ok=True)
        await emit_progress(progress_callback, 0.02)
        try:
            if plan.template == "audio_image":
                await self._audio_image(plan, output, cancel_event, progress_callback)
            elif plan.template == "slideshow":
                await self._slideshow(plan, output, cancel_event, progress_callback)
            elif plan.template in {"merge_videos", "intro_main_outro"}:
                await self._merge_videos(plan, output, workspace, cancel_event, progress_callback)
            elif plan.template == "video_audio":
                await self._video_audio(plan, output, cancel_event, progress_callback)
            elif plan.template == "logo_overlay":
                await self._logo_overlay(plan, output, cancel_event, progress_callback)
            else:
                raise ValueError(f"Unsupported FFmpeg renderer template: {plan.template}")

            logo = next((item for item in plan.assets if item.role == "logo"), None)
            if logo is not None and plan.template != "logo_overlay":
                await emit_progress(progress_callback, 0.90)
                logo_output = workspace / "with-logo.mp4"
                await self._overlay_existing_video(
                    source=output,
                    logo=logo.path,
                    destination=logo_output,
                    plan=plan,
                    cancel_event=cancel_event,
                    progress_callback=progress_callback,
                    progress_start=0.88,
                    progress_end=0.96,
                )
                logo_output.replace(output)

            await emit_progress(progress_callback, 0.96)
            result = await self._validate_output(output, plan)
            await emit_progress(progress_callback, 1.0)
            return result
        except (Exception, asyncio.CancelledError):
            output.unlink(missing_ok=True)
            raise
        finally:
            shutil.rmtree(workspace, ignore_errors=True)

    @staticmethod
    def _check_cancel(cancel_event: threading.Event | None) -> None:
        if cancel_event is not None and cancel_event.is_set():
            raise CancelledError("Media Studio render cancelled")

    async def _run_ffmpeg(
        self,
        args: list[str],
        *,
        expected_duration: float,
        cancel_event: threading.Event | None,
        progress_callback: ProgressCallback | None,
        progress_start: float = 0.02,
        progress_end: float = 0.94,
    ) -> None:
        """Run FFmpeg and translate its media clock into monotonic render progress."""
        last = progress_start
        saw_out_time_us = False

        async def consume(line: str) -> None:
            nonlocal last, saw_out_time_us
            key, separator, raw = line.partition("=")
            if not separator:
                return
            if key == "out_time_us":
                saw_out_time_us = True
            elif key != "out_time_ms" or saw_out_time_us:
                return
            try:
                rendered_seconds = max(0.0, int(raw) / 1_000_000)
            except ValueError:
                return
            ratio = min(1.0, rendered_seconds / max(expected_duration, 0.001))
            value = max(last, progress_start + (progress_end - progress_start) * ratio)
            if value - last >= 0.002 or ratio >= 1.0:
                last = value
                await emit_progress(progress_callback, value)

        command = [args[0], "-progress", "pipe:1", "-nostats", *args[1:]]
        await _run_process(
            *command,
            timeout=self.settings.render_timeout_seconds,
            cancel_event=cancel_event,
            stdout_line_callback=consume,
        )

    def _visual_filter(self, source: str, target: str, plan: RenderPlan, *, prefix: str) -> str:
        width, height, fps = plan.width, plan.height, plan.fps
        common = f"setsar=1,fps={fps},format=yuv420p"
        if plan.fit_mode == "fill":
            return (
                f"[{source}]scale={width}:{height}:force_original_aspect_ratio=increase,"
                f"crop={width}:{height},{common}[{target}]"
            )
        if plan.fit_mode == "blur-background":
            return (
                f"[{source}]split=2[{prefix}bg][{prefix}fg];"
                f"[{prefix}bg]scale={width}:{height}:force_original_aspect_ratio=increase,"
                f"crop={width}:{height},boxblur=20:10[{prefix}blur];"
                f"[{prefix}fg]scale={width}:{height}:force_original_aspect_ratio=decrease[{prefix}front];"
                f"[{prefix}blur][{prefix}front]overlay=(W-w)/2:(H-h)/2,{common}[{target}]"
            )
        return (
            f"[{source}]scale={width}:{height}:force_original_aspect_ratio=decrease,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black,{common}[{target}]"
        )

    @staticmethod
    def _video_assets(plan: RenderPlan) -> list[RenderAsset]:
        return [item for item in plan.assets if item.asset_type == "video" and item.role != "logo"]

    @staticmethod
    def _image_assets(plan: RenderPlan) -> list[RenderAsset]:
        return [item for item in plan.assets if item.asset_type == "image" and item.role != "logo"]

    @staticmethod
    def _audio_assets(plan: RenderPlan) -> list[RenderAsset]:
        return [item for item in plan.assets if item.asset_type in {"audio", "voice"} and item.role != "logo"]

    async def _audio_image(
        self,
        plan: RenderPlan,
        output: Path,
        cancel_event: threading.Event | None,
        progress_callback: ProgressCallback | None,
    ) -> None:
        image = self._image_assets(plan)[0]
        audio = self._audio_assets(plan)[0]
        filters = self._visual_filter("0:v", "v", plan, prefix="ai")
        args = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-loop",
            "1",
            "-framerate",
            str(plan.fps),
            "-i",
            str(image.path),
            "-i",
            str(audio.path),
            "-filter_complex",
            filters,
            "-map",
            "[v]",
            "-map",
            "1:a:0",
            "-t",
            f"{plan.expected_duration:.3f}",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "23",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "160k",
            "-ar",
            "48000",
            "-ac",
            "2",
            "-movflags",
            "+faststart",
            str(output),
        ]
        await self._run_ffmpeg(
            args,
            expected_duration=plan.expected_duration,
            cancel_event=cancel_event,
            progress_callback=progress_callback,
        )

    async def _slideshow(
        self,
        plan: RenderPlan,
        output: Path,
        cancel_event: threading.Event | None,
        progress_callback: ProgressCallback | None,
    ) -> None:
        images = self._image_assets(plan)
        audios = self._audio_assets(plan)
        count = len(images)
        fade = min(0.35, plan.expected_duration / max(4, count * 4)) if count > 1 else 0.0
        overlap_total = fade * (count - 1) if plan.transition == "fade" else 0.0
        image_duration = (plan.expected_duration + overlap_total) / count if count else 4.0
        image_duration = max(0.25, image_duration)
        args = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
        for image in images:
            args += [
                "-loop",
                "1",
                "-framerate",
                str(plan.fps),
                "-t",
                f"{image_duration:.3f}",
                "-i",
                str(image.path),
            ]
        audio_index = len(images)
        if audios:
            args += ["-i", str(audios[0].path)]
        else:
            args += [
                "-f",
                "lavfi",
                "-t",
                f"{plan.expected_duration:.3f}",
                "-i",
                "anullsrc=r=48000:cl=stereo",
            ]

        filters: list[str] = []
        for index in range(count):
            filters.append(self._visual_filter(f"{index}:v", f"s{index}", plan, prefix=f"s{index}"))
        if plan.transition == "fade" and count > 1:
            previous = "s0"
            for index in range(1, count):
                target = "v" if index == count - 1 else f"xf{index}"
                offset = image_duration * index - fade * index
                filters.append(
                    f"[{previous}][s{index}]xfade=transition=fade:duration={fade:.3f}:"
                    f"offset={offset:.3f}[{target}]"
                )
                previous = target
        else:
            video_labels = "".join(f"[s{index}]" for index in range(count))
            filters.append(f"{video_labels}concat=n={count}:v=1:a=0[v]")
        args += [
            "-filter_complex",
            ";".join(filters),
            "-map",
            "[v]",
            "-map",
            f"{audio_index}:a:0",
            "-t",
            f"{plan.expected_duration:.3f}",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "23",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            "-ar",
            "48000",
            "-ac",
            "2",
            "-movflags",
            "+faststart",
            str(output),
        ]
        await self._run_ffmpeg(
            args,
            expected_duration=plan.expected_duration,
            cancel_event=cancel_event,
            progress_callback=progress_callback,
        )

    async def _normalize_video(
        self,
        asset: RenderAsset,
        output: Path,
        plan: RenderPlan,
        cancel_event: threading.Event | None,
        progress_callback: ProgressCallback | None = None,
        progress_start: float = 0.02,
        progress_end: float = 0.94,
    ) -> None:
        probe = await probe_media_file(asset.path)
        args = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(asset.path)]
        audio_map = "0:a:0"
        if not probe.has_audio:
            args += [
                "-f",
                "lavfi",
                "-t",
                f"{float(asset.duration or probe.duration):.3f}",
                "-i",
                "anullsrc=r=48000:cl=stereo",
            ]
            audio_map = "1:a:0"
        args += [
            "-filter_complex",
            self._visual_filter("0:v", "v", plan, prefix=f"n{asset.asset_id}"),
            "-map",
            "[v]",
            "-map",
            audio_map,
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "23",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "160k",
            "-ar",
            "48000",
            "-ac",
            "2",
            "-af",
            "apad",
            "-t",
            f"{float(asset.duration or probe.duration):.3f}",
            "-movflags",
            "+faststart",
            str(output),
        ]
        await self._run_ffmpeg(
            args,
            expected_duration=float(asset.duration or probe.duration),
            cancel_event=cancel_event,
            progress_callback=progress_callback,
            progress_start=progress_start,
            progress_end=progress_end,
        )

    async def _merge_videos(
        self,
        plan: RenderPlan,
        output: Path,
        workspace: Path,
        cancel_event: threading.Event | None,
        progress_callback: ProgressCallback | None,
    ) -> None:
        videos = self._video_assets(plan)
        if plan.template == "intro_main_outro":
            role_order = {"intro": 0, "main": 1, "outro": 2}
            videos = sorted(videos, key=lambda item: (role_order.get(item.role, 1), item.position))
        normalized: list[Path] = []
        for index, asset in enumerate(videos):
            self._check_cancel(cancel_event)
            target = workspace / f"normalized-{index:03d}.mp4"
            start = 0.03 + 0.72 * (index / max(1, len(videos)))
            end = 0.03 + 0.72 * ((index + 1) / max(1, len(videos)))
            await self._normalize_video(
                asset,
                target,
                plan,
                cancel_event,
                progress_callback,
                start,
                end,
            )
            normalized.append(target)
        concat_file = workspace / "concat.txt"
        concat_file.write_text(
            "".join(f"file '{str(path).replace(chr(39), chr(39) + chr(92) + chr(39) + chr(39))}'\n" for path in normalized),
            encoding="utf-8",
        )
        args = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(concat_file),
            "-c",
            "copy",
            "-movflags",
            "+faststart",
            str(output),
        ]
        await self._run_ffmpeg(
            args,
            expected_duration=plan.expected_duration,
            cancel_event=cancel_event,
            progress_callback=progress_callback,
            progress_start=0.76,
            progress_end=0.94,
        )

    async def _video_audio(
        self,
        plan: RenderPlan,
        output: Path,
        cancel_event: threading.Event | None,
        progress_callback: ProgressCallback | None,
    ) -> None:
        video = self._video_assets(plan)[0]
        audio = self._audio_assets(plan)[0]
        probe = await probe_media_file(video.path)
        filters = [self._visual_filter("0:v", "v", plan, prefix="va")]
        if plan.audio_mode in {"mix_audio", "background_music"} and probe.has_audio:
            new_volume = 0.20 if plan.audio_mode == "background_music" else 1.0
            filters += [
                "[0:a:0]aresample=48000,aformat=channel_layouts=stereo[a0]",
                f"[1:a:0]volume={new_volume:.2f},aresample=48000,aformat=channel_layouts=stereo[a1]",
                "[a0][a1]amix=inputs=2:duration=first:dropout_transition=2,apad[a]",
            ]
        else:
            filters.append("[1:a:0]aresample=48000,aformat=channel_layouts=stereo,apad[a]")
        args = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(video.path),
            "-i",
            str(audio.path),
            "-filter_complex",
            ";".join(filters),
            "-map",
            "[v]",
            "-map",
            "[a]",
            "-t",
            f"{plan.expected_duration:.3f}",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "23",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "160k",
            "-movflags",
            "+faststart",
            str(output),
        ]
        await self._run_ffmpeg(
            args,
            expected_duration=plan.expected_duration,
            cancel_event=cancel_event,
            progress_callback=progress_callback,
        )

    @staticmethod
    def _overlay_expression(position: str) -> tuple[str, str]:
        margin = "32"
        positions = {
            "top-left": (margin, margin),
            "top-right": (f"W-w-{margin}", margin),
            "bottom-left": (margin, f"H-h-{margin}"),
            "bottom-right": (f"W-w-{margin}", f"H-h-{margin}"),
            "center": ("(W-w)/2", "(H-h)/2"),
        }
        return positions.get(position, positions["top-right"])

    async def _overlay_existing_video(
        self,
        *,
        source: Path,
        logo: Path,
        destination: Path,
        plan: RenderPlan,
        cancel_event: threading.Event | None,
        progress_callback: ProgressCallback | None = None,
        progress_start: float = 0.02,
        progress_end: float = 0.94,
    ) -> None:
        x, y = self._overlay_expression(plan.logo_position)
        probe = await probe_media_file(source)
        args = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(source),
            "-i",
            str(logo),
            "-filter_complex",
            f"[1:v]scale='min(240,min(iw,{max(1, plan.width - 64)}))':"
            f"'min(240,min(ih,{max(1, plan.height - 64)}))':"
            f"force_original_aspect_ratio=decrease[logo];[0:v][logo]overlay={x}:{y}[v]",
            "-map",
            "[v]",
        ]
        if probe.has_audio:
            args += ["-map", "0:a:0", "-c:a", "aac", "-b:a", "160k"]
        else:
            args += ["-an"]
        args += [
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "23",
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            str(destination),
        ]
        await self._run_ffmpeg(
            args,
            expected_duration=probe.duration,
            cancel_event=cancel_event,
            progress_callback=progress_callback,
            progress_start=progress_start,
            progress_end=progress_end,
        )

    async def _logo_overlay(
        self,
        plan: RenderPlan,
        output: Path,
        cancel_event: threading.Event | None,
        progress_callback: ProgressCallback | None,
    ) -> None:
        video = self._video_assets(plan)[0]
        logo = next((item for item in plan.assets if item.role == "logo"), None)
        if logo is None:
            raise ValueError("Logo Overlay requires an asset with role=logo")
        normalized = output.with_name("base-video.mp4")
        try:
            await self._normalize_video(
                video,
                normalized,
                plan,
                cancel_event,
                progress_callback,
                0.02,
                0.70,
            )
            await self._overlay_existing_video(
                source=normalized,
                logo=logo.path,
                destination=output,
                plan=plan,
                cancel_event=cancel_event,
                progress_callback=progress_callback,
                progress_start=0.70,
                progress_end=0.94,
            )
        finally:
            normalized.unlink(missing_ok=True)

    async def _validate_output(self, output: Path, plan: RenderPlan) -> RenderResult:
        if not output.exists() or output.stat().st_size <= 0:
            raise FFmpegError("Renderer did not produce an output file")
        probe = await probe_media_file(output)
        if not probe.has_video or probe.duration <= 0:
            raise FFmpegError("Rendered output is not a valid video")
        if probe.width != plan.width or probe.height != plan.height:
            raise FFmpegError(
                f"Rendered dimensions mismatch: expected {plan.width}x{plan.height}, "
                f"got {probe.width}x{probe.height}"
            )
        if "mp4" not in (probe.format_name or "").lower():
            raise FFmpegError("Rendered output is not a playable MP4 container")
        expects_audio = plan.template in {
            "audio_image",
            "slideshow",
            "merge_videos",
            "video_audio",
            "intro_main_outro",
        }
        if expects_audio and not probe.has_audio:
            raise FFmpegError("Rendered output is missing its expected audio stream")
        tolerance = max(1.0, min(3.0, plan.expected_duration * 0.05))
        if abs(probe.duration - plan.expected_duration) > tolerance:
            raise FFmpegError(
                f"Rendered duration mismatch: expected {plan.expected_duration:.2f}s, got {probe.duration:.2f}s"
            )
        return RenderResult(
            output_path=output,
            duration=probe.duration,
            file_size=output.stat().st_size,
            has_audio=probe.has_audio,
        )


ffmpeg_renderer = FFmpegRenderer()
