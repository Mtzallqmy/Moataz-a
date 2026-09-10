from __future__ import annotations

import asyncio
import subprocess
import threading
from pathlib import Path

import pytest

from app.config import Settings
from app.errors import CancelledError, FFmpegError
from app.services.composer import RenderAsset, RenderPlan
from app.services.media import probe_media_file
from app.services.renderers.ffmpeg_renderer import FFmpegRenderer


def _run(*args: str) -> None:
    subprocess.run(args, check=True, capture_output=True)


def _image(path: Path) -> Path:
    _run(
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-f",
        "lavfi",
        "-i",
        "color=c=white:s=320x180",
        "-frames:v",
        "1",
        str(path),
    )
    return path


def _audio(path: Path, duration: float = 1.2) -> Path:
    _run(
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-f",
        "lavfi",
        "-i",
        f"sine=frequency=440:duration={duration}",
        "-c:a",
        "libmp3lame",
        str(path),
    )
    return path


def _video(path: Path, duration: float = 1.0, frequency: int = 440) -> Path:
    _run(
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-f",
        "lavfi",
        "-i",
        f"color=c=blue:s=320x180:d={duration}",
        "-f",
        "lavfi",
        "-i",
        f"sine=frequency={frequency}:duration={duration}",
        "-shortest",
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        str(path),
    )
    return path


def _custom_video(
    path: Path,
    *,
    duration: float,
    size: str,
    fps: int,
    with_audio: bool,
) -> Path:
    args = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-f",
        "lavfi",
        "-i",
        f"testsrc=size={size}:rate={fps}:duration={duration}",
    ]
    if with_audio:
        args += ["-f", "lavfi", "-i", f"sine=frequency=330:duration={duration}", "-shortest"]
    args += ["-c:v", "libx264", "-pix_fmt", "yuv420p"]
    if with_audio:
        args += ["-c:a", "aac"]
    args.append(str(path))
    _run(*args)
    return path


def _asset(
    asset_id: int,
    path: Path,
    asset_type: str,
    *,
    duration: float | None = None,
    role: str = "main",
    position: int = 0,
) -> RenderAsset:
    return RenderAsset(
        asset_id=asset_id,
        asset_type=asset_type,
        role=role,
        position=position,
        path=path,
        duration=duration,
        width=320 if asset_type in {"image", "video"} else None,
        height=180 if asset_type in {"image", "video"} else None,
        metadata={"has_audio": asset_type in {"audio", "voice", "video"}},
    )


def _plan(template: str, assets: list[RenderAsset], duration: float, **overrides: str) -> RenderPlan:
    return RenderPlan(
        project_id=7001,
        user_id=8001,
        template=template,
        width=360,
        height=640,
        fps=24,
        aspect_ratio="9:16",
        fit_mode=overrides.get("fit_mode", "fit"),
        transition=overrides.get("transition", "none"),
        audio_mode=overrides.get("audio_mode", "replace_audio"),
        logo_position=overrides.get("logo_position", "top-right"),
        assets=tuple(assets),
        expected_duration=duration,
    )


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        project_dir=tmp_path / "projects",
        render_temp_dir=tmp_path / "tmp",
        render_timeout_seconds=90,
        max_project_duration_seconds=300,
        max_render_duration_seconds=300,
    )


@pytest.mark.asyncio
async def test_acceptance_audio_plus_image(tmp_path: Path) -> None:
    image = _image(tmp_path / "cover.jpg")
    audio = _audio(tmp_path / "audio.mp3", 1.2)
    renderer = FFmpegRenderer(_settings(tmp_path))
    result = await renderer.render(
        _plan("audio_image", [_asset(1, image, "image"), _asset(2, audio, "audio", duration=1.2)], 1.2),
        render_job_id=1,
    )
    probe = await probe_media_file(result.output_path)
    assert probe.has_video and probe.has_audio
    assert probe.duration == pytest.approx(1.2, abs=0.35)


@pytest.mark.asyncio
async def test_acceptance_slideshow_with_audio_and_fade(tmp_path: Path) -> None:
    images = [_image(tmp_path / f"image-{index}.jpg") for index in range(3)]
    audio = _audio(tmp_path / "slide.mp3", 1.8)
    assets = [_asset(index + 1, path, "image", position=index) for index, path in enumerate(images)]
    assets.append(_asset(10, audio, "audio", duration=1.8, position=3))
    renderer = FFmpegRenderer(_settings(tmp_path))
    result = await renderer.render(
        _plan("slideshow", assets, 1.8, transition="fade"),
        render_job_id=2,
    )
    probe = await probe_media_file(result.output_path)
    assert probe.has_video and probe.has_audio
    assert probe.duration == pytest.approx(1.8, abs=0.4)


@pytest.mark.asyncio
async def test_acceptance_three_image_slideshow_without_audio_is_about_twelve_seconds(
    tmp_path: Path,
) -> None:
    images = [_image(tmp_path / f"silent-{index}.jpg") for index in range(3)]
    assets = [_asset(index + 1, path, "image", position=index) for index, path in enumerate(images)]
    renderer = FFmpegRenderer(_settings(tmp_path))
    result = await renderer.render(_plan("slideshow", assets, 12.0), render_job_id=20)
    probe = await probe_media_file(result.output_path)
    assert probe.has_video and probe.has_audio
    assert probe.duration == pytest.approx(12.0, abs=0.5)


@pytest.mark.asyncio
async def test_acceptance_merge_videos_normalizes_inputs(tmp_path: Path) -> None:
    first = _video(tmp_path / "one.mp4", 0.9, 440)
    second = _video(tmp_path / "two.mp4", 1.1, 660)
    renderer = FFmpegRenderer(_settings(tmp_path))
    result = await renderer.render(
        _plan(
            "merge_videos",
            [_asset(1, first, "video", duration=0.9), _asset(2, second, "video", duration=1.1, position=1)],
            2.0,
            fit_mode="fill",
        ),
        render_job_id=3,
    )
    probe = await probe_media_file(result.output_path)
    assert probe.has_video and probe.has_audio
    assert probe.duration == pytest.approx(2.0, abs=0.5)


@pytest.mark.asyncio
async def test_merge_mismatched_fps_dimensions_and_missing_audio(tmp_path: Path) -> None:
    first = _custom_video(
        tmp_path / "wide-silent.mp4", duration=0.8, size="426x240", fps=15, with_audio=False
    )
    second = _custom_video(
        tmp_path / "square-audio.mp4", duration=1.0, size="240x240", fps=30, with_audio=True
    )
    renderer = FFmpegRenderer(_settings(tmp_path))
    result = await renderer.render(
        _plan(
            "merge_videos",
            [
                _asset(1, first, "video", duration=0.8),
                _asset(2, second, "video", duration=1.0, position=1),
            ],
            1.8,
        ),
        render_job_id=21,
    )
    probe = await probe_media_file(result.output_path)
    assert probe.has_video and probe.has_audio
    assert (probe.width, probe.height) == (360, 640)
    assert probe.duration == pytest.approx(1.8, abs=0.5)


@pytest.mark.asyncio
@pytest.mark.parametrize("audio_mode", ["replace_audio", "mix_audio", "background_music"])
async def test_acceptance_video_plus_audio_modes(tmp_path: Path, audio_mode: str) -> None:
    video = _video(tmp_path / f"video-{audio_mode}.mp4", 1.2)
    audio = _audio(tmp_path / f"new-{audio_mode}.mp3", 1.2)
    renderer = FFmpegRenderer(_settings(tmp_path))
    result = await renderer.render(
        _plan(
            "video_audio",
            [_asset(1, video, "video", duration=1.2), _asset(2, audio, "audio", duration=1.2)],
            1.2,
            audio_mode=audio_mode,
        ),
        render_job_id={"replace_audio": 4, "mix_audio": 5, "background_music": 6}[audio_mode],
    )
    probe = await probe_media_file(result.output_path)
    assert probe.has_video and probe.has_audio


@pytest.mark.asyncio
async def test_acceptance_intro_main_outro_and_logo(tmp_path: Path) -> None:
    intro = _video(tmp_path / "intro.mp4", 0.7, 330)
    main = _video(tmp_path / "main.mp4", 0.9, 440)
    outro = _video(tmp_path / "outro.mp4", 0.7, 550)
    logo = _image(tmp_path / "logo.png")
    assets = [
        _asset(1, intro, "video", duration=0.7, role="intro", position=0),
        _asset(2, main, "video", duration=0.9, role="main", position=1),
        _asset(3, outro, "video", duration=0.7, role="outro", position=2),
        _asset(4, logo, "image", role="logo", position=3),
    ]
    renderer = FFmpegRenderer(_settings(tmp_path))
    result = await renderer.render(
        _plan("intro_main_outro", assets, 2.3, logo_position="bottom-right"),
        render_job_id=7,
    )
    probe = await probe_media_file(result.output_path)
    assert probe.has_video and probe.has_audio
    assert probe.duration == pytest.approx(2.3, abs=0.6)


@pytest.mark.asyncio
async def test_acceptance_logo_overlay_template(tmp_path: Path) -> None:
    video = _video(tmp_path / "logo-video.mp4", 1.0)
    logo = _image(tmp_path / "overlay.png")
    renderer = FFmpegRenderer(_settings(tmp_path))
    result = await renderer.render(
        _plan(
            "logo_overlay",
            [_asset(1, video, "video", duration=1.0), _asset(2, logo, "image", role="logo", position=1)],
            1.0,
            fit_mode="blur-background",
        ),
        render_job_id=8,
    )
    probe = await probe_media_file(result.output_path)
    assert probe.has_video
    assert probe.duration == pytest.approx(1.0, abs=0.35)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("width", "height", "aspect", "fit_mode"),
    [
        (360, 640, "9:16", "fit"),
        (640, 360, "16:9", "fill"),
        (360, 360, "1:1", "blur-background"),
    ],
)
async def test_aspect_ratio_and_fit_mode_outputs_are_exact(
    tmp_path: Path,
    width: int,
    height: int,
    aspect: str,
    fit_mode: str,
) -> None:
    image = _image(tmp_path / f"aspect-{aspect.replace(':', '-')}.jpg")
    audio = _audio(tmp_path / f"aspect-{fit_mode}.mp3", 0.7)
    plan = _plan(
        "audio_image",
        [_asset(1, image, "image"), _asset(2, audio, "audio", duration=0.7)],
        0.7,
        fit_mode=fit_mode,
    )
    plan = RenderPlan(
        project_id=plan.project_id,
        user_id=plan.user_id,
        template=plan.template,
        width=width,
        height=height,
        fps=plan.fps,
        aspect_ratio=aspect,
        fit_mode=plan.fit_mode,
        transition=plan.transition,
        audio_mode=plan.audio_mode,
        logo_position=plan.logo_position,
        assets=plan.assets,
        expected_duration=plan.expected_duration,
    )
    result = await FFmpegRenderer(_settings(tmp_path)).render(
        plan,
        render_job_id=30 + width + height,
    )
    probe = await probe_media_file(result.output_path)
    assert (probe.width, probe.height) == (width, height)


@pytest.mark.asyncio
async def test_ffmpeg_clock_progress_is_monotonic_and_not_only_fixed_milestones(tmp_path: Path) -> None:
    image = _image(tmp_path / "progress.jpg")
    audio = _audio(tmp_path / "progress.mp3", 2.5)
    values: list[float] = []
    await FFmpegRenderer(_settings(tmp_path)).render(
        _plan(
            "audio_image",
            [_asset(1, image, "image"), _asset(2, audio, "audio", duration=2.5)],
            2.5,
        ),
        render_job_id=41,
        progress_callback=values.append,
    )
    assert values == sorted(values)
    assert any(0.02 < value < 0.94 for value in values)


@pytest.mark.asyncio
async def test_cancel_active_ffmpeg_removes_partial_output_and_workspace(tmp_path: Path) -> None:
    image = _image(tmp_path / "cancel.jpg")
    audio = _audio(tmp_path / "cancel.mp3", 20)
    renderer = FFmpegRenderer(_settings(tmp_path))
    event = threading.Event()
    task = asyncio.create_task(
        renderer.render(
            _plan(
                "audio_image",
                [_asset(1, image, "image"), _asset(2, audio, "audio", duration=20)],
                20,
            ),
            render_job_id=42,
            cancel_event=event,
        )
    )
    await asyncio.sleep(0.03)
    event.set()
    with pytest.raises(CancelledError):
        await task
    assert not (tmp_path / "projects" / "7001" / "renders" / "42" / "output.mp4").exists()
    assert not (tmp_path / "tmp" / "render-42").exists()


@pytest.mark.asyncio
async def test_output_validation_rejects_video_without_expected_audio(tmp_path: Path) -> None:
    output = _custom_video(
        tmp_path / "silent.mp4", duration=1.0, size="360x640", fps=24, with_audio=False
    )
    plan = _plan("audio_image", [], 1.0)
    with pytest.raises(FFmpegError, match="expected audio"):
        await FFmpegRenderer(_settings(tmp_path))._validate_output(output, plan)
