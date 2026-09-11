from __future__ import annotations

from typing import Any

STYLE_PRESETS: dict[str, dict[str, Any]] = {
    "reels-fast": {
        "pacing": "fast",
        "transition": "slide",
        "transition_duration": 0.22,
        "caption_preset": "reels-bold",
        "music_level": 0.18,
        "zoom_frequency": 0.7,
        "clip_duration": 2.5,
        "typography": "bold",
    },
    "cinematic": {
        "pacing": "slow",
        "transition": "dissolve",
        "transition_duration": 0.8,
        "caption_preset": "cinematic",
        "music_level": 0.22,
        "zoom_frequency": 0.2,
        "clip_duration": 6.0,
        "typography": "elegant",
    },
    "podcast": {
        "pacing": "medium",
        "transition": "fade",
        "transition_duration": 0.3,
        "caption_preset": "podcast",
        "music_level": 0.1,
        "zoom_frequency": 0.15,
        "clip_duration": 8.0,
        "typography": "clear",
    },
    "product-ad": {
        "pacing": "fast",
        "transition": "zoom",
        "transition_duration": 0.3,
        "caption_preset": "product",
        "music_level": 0.2,
        "zoom_frequency": 0.6,
        "clip_duration": 3.0,
        "typography": "bold",
    },
    "minimal": {
        "pacing": "medium",
        "transition": "fade",
        "transition_duration": 0.35,
        "caption_preset": "minimal",
        "music_level": 0.14,
        "zoom_frequency": 0.0,
        "clip_duration": 5.0,
        "typography": "clean",
    },
    "documentary": {
        "pacing": "medium",
        "transition": "dissolve",
        "transition_duration": 0.5,
        "caption_preset": "documentary",
        "music_level": 0.12,
        "zoom_frequency": 0.15,
        "clip_duration": 6.0,
        "typography": "clear",
    },
    "news": {
        "pacing": "fast",
        "transition": "wipe",
        "transition_duration": 0.2,
        "caption_preset": "news",
        "music_level": 0.08,
        "zoom_frequency": 0.0,
        "clip_duration": 4.0,
        "typography": "news",
    },
    "motivational": {
        "pacing": "medium-fast",
        "transition": "push",
        "transition_duration": 0.3,
        "caption_preset": "motivational",
        "music_level": 0.2,
        "zoom_frequency": 0.4,
        "clip_duration": 4.0,
        "typography": "bold",
    },
}


CAPTION_PRESETS: dict[str, dict[str, Any]] = {
    "reels-bold": {"font_size": 58, "color": "white", "highlight_color": "yellow", "background": "black@0.55", "position": "bottom", "animation": "pop", "rtl": True},
    "cinematic": {"font_size": 42, "color": "white", "highlight_color": "gold", "background": "black@0.35", "position": "bottom", "animation": "fade", "rtl": True},
    "podcast": {"font_size": 52, "color": "white", "highlight_color": "cyan", "background": "black@0.6", "position": "bottom", "animation": "fade", "rtl": True},
    "product": {"font_size": 54, "color": "white", "highlight_color": "yellow", "background": "black@0.5", "position": "center", "animation": "pop", "rtl": True},
    "minimal": {"font_size": 40, "color": "white", "highlight_color": "white", "background": "black@0.3", "position": "bottom", "animation": "fade", "rtl": True},
    "documentary": {"font_size": 42, "color": "white", "highlight_color": "orange", "background": "black@0.5", "position": "bottom", "animation": "fade", "rtl": True},
    "news": {"font_size": 48, "color": "white", "highlight_color": "red", "background": "navy@0.7", "position": "bottom", "animation": "none", "rtl": True},
    "motivational": {"font_size": 56, "color": "white", "highlight_color": "yellow", "background": "black@0.45", "position": "center", "animation": "pop", "rtl": True},
}


def style(name: str) -> dict[str, Any]:
    try:
        return dict(STYLE_PRESETS[name])
    except KeyError as exc:
        raise ValueError(f"Unknown editing style: {name}") from exc


def caption_style(name: str) -> dict[str, Any]:
    try:
        return dict(CAPTION_PRESETS[name])
    except KeyError as exc:
        raise ValueError(f"Unknown caption preset: {name}") from exc
