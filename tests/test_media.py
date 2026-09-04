import pytest

from app.services.media import target_total_bitrate, video_height_for_bitrate


def test_target_total_bitrate_uses_file_budget():
    bitrate = target_total_bitrate(49 * 1024 * 1024, 600)
    assert 600_000 < bitrate < 700_000


def test_target_total_bitrate_rejects_invalid_inputs():
    with pytest.raises(ValueError):
        target_total_bitrate(0, 60)
    with pytest.raises(ValueError):
        target_total_bitrate(1000, 0)


def test_video_height_for_bitrate_scales_down_conservatively():
    assert video_height_for_bitrate(400_000) == 360
    assert video_height_for_bitrate(700_000) == 480
    assert video_height_for_bitrate(1_200_000) == 720
    assert video_height_for_bitrate(2_500_000) == 1080
