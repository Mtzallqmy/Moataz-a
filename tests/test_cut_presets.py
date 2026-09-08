import pytest

from app.services.cutting import resolve_cut_plan


def test_free_cut_keeps_explicit_range():
    plan = resolve_cut_plan(start=10, end=47.5, preset="free", mode="PRECISE", source_duration=120)
    assert plan.start == 10
    assert plan.end == 47.5
    assert plan.duration == 37.5
    assert plan.mode == "PRECISE"
    assert plan.preset == "free"


def test_story_30_cut_is_exactly_30_seconds():
    plan = resolve_cut_plan(start=12, preset="30", mode="FAST", source_duration=90)
    assert plan.start == 12
    assert plan.end == 42
    assert plan.duration == 30
    assert plan.mode == "FAST"


def test_60_second_cut_is_exactly_60_seconds():
    plan = resolve_cut_plan(start=5, preset="60", source_duration=100)
    assert plan.end == 65
    assert plan.duration == 60


@pytest.mark.parametrize(
    "kwargs",
    [
        {"start": -1, "end": 3, "preset": "free", "source_duration": 10},
        {"start": 5, "end": 5, "preset": "free", "source_duration": 10},
        {"start": 1, "end": None, "preset": "free", "source_duration": 10},
        {"start": 1, "preset": "invalid", "source_duration": 10},
        {"start": 1, "preset": "30", "source_duration": 20},
        {"start": 50, "preset": "60", "source_duration": 100},
    ],
)
def test_cut_presets_reject_invalid_or_inexact_ranges(kwargs):
    with pytest.raises(ValueError):
        resolve_cut_plan(**kwargs)


def test_cut_mode_is_strict():
    with pytest.raises(ValueError):
        resolve_cut_plan(start=0, end=5, preset="free", mode="turbo", source_duration=10)
