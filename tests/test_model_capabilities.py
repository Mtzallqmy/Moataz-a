from app.services.model_capabilities import extract_capabilities, extract_modalities


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
