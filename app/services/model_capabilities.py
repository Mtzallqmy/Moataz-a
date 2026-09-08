from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

_CAPABILITY_ORDER = ("text", "vision", "image", "video", "audio", "code", "tools", "embeddings")
_CODE_RE = re.compile(r"(?:^|[/:._-])(code|coder|coding|devstral|codestral|swe)(?:$|[/:._-])", re.IGNORECASE)
_VISION_RE = re.compile(r"(?:^|[/:._-])(vision|vl|multimodal|omni)(?:$|[/:._-])", re.IGNORECASE)
_IMAGE_RE = re.compile(r"(?:^|[/:._-])(image|flux|sdxl|stable-diffusion|recraft|imagen)(?:$|[/:._-])", re.IGNORECASE)
_VIDEO_RE = re.compile(r"(?:^|[/:._-])(video|veo|kling|hailuo|wan)(?:$|[/:._-])", re.IGNORECASE)
_AUDIO_RE = re.compile(r"(?:^|[/:._-])(audio|speech|voice|tts|whisper|realtime)(?:$|[/:._-])", re.IGNORECASE)
_EMBED_RE = re.compile(r"(?:^|[/:._-])(embed|embedding)(?:$|[/:._-])", re.IGNORECASE)


def _flatten_strings(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        values: list[str] = []
        for key, item in value.items():
            if item is True:
                values.append(str(key))
            elif isinstance(item, (str, list, tuple, set, dict)):
                values.extend(_flatten_strings(item))
        return values
    if isinstance(value, Iterable) and not isinstance(value, (bytes, bytearray)):
        values = []
        for item in value:
            values.extend(_flatten_strings(item))
        return values
    return [str(value)]


def _normalize_modality(value: str) -> str | None:
    token = value.strip().lower().replace("_", "-")
    mapping = {
        "text": "text",
        "input-text": "text",
        "output-text": "text",
        "image": "image",
        "images": "image",
        "image-url": "image",
        "vision": "image",
        "video": "video",
        "videos": "video",
        "audio": "audio",
        "speech": "audio",
        "voice": "audio",
    }
    if token in mapping:
        return mapping[token]
    if "image" in token or "vision" in token:
        return "image"
    if "video" in token:
        return "video"
    if "audio" in token or "speech" in token or "voice" in token:
        return "audio"
    if "text" in token:
        return "text"
    return None


def _modalities_from(value: object) -> tuple[str, ...]:
    found = {_normalize_modality(item) for item in _flatten_strings(value)}
    found.discard(None)
    return tuple(item for item in ("text", "image", "video", "audio") if item in found)


def _architecture(item: dict[str, Any]) -> dict[str, Any]:
    value = item.get("architecture")
    return value if isinstance(value, dict) else {}


def extract_modalities(item: dict[str, Any]) -> tuple[tuple[str, ...], tuple[str, ...]]:
    architecture = _architecture(item)
    input_values: list[object] = [
        item.get("input_modalities"),
        item.get("inputModalities"),
        architecture.get("input_modalities"),
        architecture.get("inputModalities"),
    ]
    output_values: list[object] = [
        item.get("output_modalities"),
        item.get("outputModalities"),
        architecture.get("output_modalities"),
        architecture.get("outputModalities"),
    ]

    shared = item.get("modalities") or item.get("supported_modalities")
    inputs: set[str] = set()
    outputs: set[str] = set()
    for value in input_values:
        inputs.update(_modalities_from(value))
    for value in output_values:
        outputs.update(_modalities_from(value))
    if shared:
        shared_modalities = set(_modalities_from(shared))
        if not inputs:
            inputs.update(shared_modalities)
        if not outputs and "text" in shared_modalities:
            outputs.add("text")

    model_id = str(item.get("id") or item.get("model") or "")
    if _VISION_RE.search(model_id):
        inputs.update({"text", "image"})
        outputs.add("text")
    if _IMAGE_RE.search(model_id):
        inputs.add("text")
        outputs.add("image")
    if _VIDEO_RE.search(model_id):
        inputs.add("text")
        outputs.add("video")
    if _AUDIO_RE.search(model_id):
        inputs.add("text")
        outputs.add("audio")
    if not inputs:
        inputs.add("text")
    if not outputs and not _EMBED_RE.search(model_id):
        outputs.add("text")

    ordered_inputs = tuple(item for item in ("text", "image", "video", "audio") if item in inputs)
    ordered_outputs = tuple(item for item in ("text", "image", "video", "audio") if item in outputs)
    return ordered_inputs, ordered_outputs


def extract_capabilities(item: dict[str, Any]) -> tuple[str, ...]:
    model_id = str(item.get("id") or item.get("model") or "")
    inputs, outputs = extract_modalities(item)
    capabilities: set[str] = set()

    if "text" in inputs or "text" in outputs:
        capabilities.add("text")
    if "image" in inputs:
        capabilities.add("vision")
    for modality in ("image", "video", "audio"):
        if modality in outputs:
            capabilities.add(modality)
    if _CODE_RE.search(model_id):
        capabilities.add("code")
    if _EMBED_RE.search(model_id):
        capabilities.add("embeddings")

    feature_values = _flatten_strings(
        [
            item.get("capabilities"),
            item.get("supported_features"),
            item.get("supported_parameters"),
            item.get("features"),
        ]
    )
    lowered = {value.strip().lower().replace("_", "-") for value in feature_values}
    if any("tool" in value or "function" in value for value in lowered):
        capabilities.add("tools")
    if any("code" in value or "coding" in value for value in lowered):
        capabilities.add("code")
    if any("vision" in value or "image-input" in value for value in lowered):
        capabilities.add("vision")

    return tuple(capability for capability in _CAPABILITY_ORDER if capability in capabilities)


def capability_icon(capability: str) -> str:
    return {
        "text": "💬",
        "vision": "👁️",
        "image": "🖼️",
        "video": "🎬",
        "audio": "🔊",
        "code": "🧑‍💻",
        "tools": "🧰",
        "embeddings": "🧠",
    }.get(capability, "✨")
