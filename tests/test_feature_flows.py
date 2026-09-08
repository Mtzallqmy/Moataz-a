import pytest


def _callbacks(markup):
    return [button.callback_data for row in markup.inline_keyboard for button in row if button.callback_data]


def test_media_keyboard_exposes_real_quality_audio_and_cut_actions():
    pytest.importorskip("aiogram")
    from app.bot.features import media_keyboard

    callbacks = _callbacks(media_keyboard(9, [360, 720, 1080]))
    assert "q:9:360" in callbacks
    assert "q:9:720" in callbacks
    assert "q:9:1080" in callbacks
    assert "q:9:best" in callbacks
    assert "q:9:audio" in callbacks
    assert "cutmenu:9:PRECISE" in callbacks


def test_video_keyboard_can_hide_audio_but_keep_cutting():
    pytest.importorskip("aiogram")
    from app.bot.features import media_keyboard

    callbacks = _callbacks(media_keyboard(3, [480, 720], include_audio=False))
    assert "q:3:audio" not in callbacks
    assert "q:3:best" in callbacks
    assert "cutmenu:3:PRECISE" in callbacks


def test_cut_menu_has_free_story_30_and_60_presets():
    pytest.importorskip("aiogram")
    from app.bot.features import cut_menu_keyboard

    callbacks = _callbacks(cut_menu_keyboard(7, "PRECISE"))
    assert "cutpick:7:free:PRECISE" in callbacks
    assert "cutpick:7:30:PRECISE" in callbacks
    assert "cutpick:7:60:PRECISE" in callbacks
    assert "cutmenu:7:FAST" in callbacks


def test_fast_cut_menu_preserves_selected_mode():
    pytest.importorskip("aiogram")
    from app.bot.features import cut_menu_keyboard

    callbacks = _callbacks(cut_menu_keyboard(7, "FAST"))
    assert "cutpick:7:free:FAST" in callbacks
    assert "cutpick:7:30:FAST" in callbacks
    assert "cutpick:7:60:FAST" in callbacks
    assert "cutmenu:7:PRECISE" in callbacks
