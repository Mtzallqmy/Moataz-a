from app.services.model_capabilities import (
    extract_capabilities,
    extract_modalities,
    extract_runware_capabilities,
)


def test_openrouter_style_architecture_detects_vision_and_text():
    item = {
        "id": "vendor/model",
        "architecture": {
            "input_modalities": ["text", "image"],
            "output_modalities": ["text"],
        },
        "supported_parameters": ["tools", "tool_choice"],
    }

    inputs, outputs = extract_modalities(item)
    capabilities = extract_capabilities(item)

    assert inputs == ("text", "image")
    assert outputs == ("text",)
    assert {"text", "vision", "tools"} <= set(capabilities)


def test_model_id_heuristics_detect_coding_and_media_generation():
    assert "code" in extract_capabilities({"id": "acme/super-coder"})
    assert "image" in extract_capabilities({"id": "acme/image-generator"})
    assert "video" in extract_capabilities({"id": "acme/video-v2"})
    assert "audio" in extract_capabilities({"id": "acme/tts-voice"})


def test_output_modalities_distinguish_vision_from_image_generation():
    vision = extract_capabilities(
        {
            "id": "vision-reader",
            "input_modalities": ["text", "image"],
            "output_modalities": ["text"],
        }
    )
    generator = extract_capabilities(
        {
            "id": "image-maker",
            "input_modalities": ["text"],
            "output_modalities": ["image"],
        }
    )

    assert "vision" in vision
    assert "image" not in vision
    assert "image" in generator


def test_runware_io_taxonomy_maps_input_and_output_modalities():
    capabilities, inputs, outputs = extract_runware_capabilities(
        {
            "air": "vendor:model@1",
            "name": "Multimodal Model",
            "capabilities": ["io:text-to-text", "io:image-to-text", "op:tool-calling"],
        }
    )

    assert inputs == ("text", "image")
    assert outputs == ("text",)
    assert {"text", "vision", "tools"} <= set(capabilities)


def test_runware_output_categories_map_media_generation():
    image_caps, _, image_outputs = extract_runware_capabilities(
        {"air": "vendor:image@1", "capabilities": ["io:text-to-image"]}
    )
    video_caps, _, video_outputs = extract_runware_capabilities(
        {"air": "vendor:video@1", "capabilities": ["io:image-to-video"]}
    )
    audio_caps, _, audio_outputs = extract_runware_capabilities(
        {"air": "vendor:audio@1", "capabilities": ["io:text-to-audio"]}
    )

    assert "image" in image_caps and image_outputs == ("image",)
    assert "video" in video_caps and video_outputs == ("video",)
    assert "audio" in audio_caps and audio_outputs == ("audio",)
