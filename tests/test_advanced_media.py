import pytest


def _callbacks(markup):
    return [button.callback_data for row in markup.inline_keyboard for button in row if button.callback_data]


def test_public_menu_exposes_saved_links_and_cut_split():
    pytest.importorskip("aiogram")
    from app.bot.advanced_media import public_menu_keyboard

    callbacks = _callbacks(public_menu_keyboard("ar"))
    assert "menu:cut" in callbacks
    assert "menu:saved" in callbacks
    assert "menu:ai" in callbacks


def test_advanced_cut_menu_exposes_continuous_30_and_60_second_split():
    pytest.importorskip("aiogram")
    from app.bot.advanced_media import advanced_cut_menu_keyboard

    callbacks = _callbacks(advanced_cut_menu_keyboard(12, "PRECISE"))
    assert "cutpick:12:free:PRECISE" in callbacks
    assert "cutpick:12:30:PRECISE" in callbacks
    assert "cutpick:12:60:PRECISE" in callbacks
    assert "advsplit:12:30" in callbacks
    assert "advsplit:12:60" in callbacks
    assert "reusemenu:12" in callbacks


def test_split_choice_supports_video_audio_full_and_custom_range():
    pytest.importorskip("aiogram")
    from app.bot.advanced_media import split_choice_keyboard

    callbacks = _callbacks(split_choice_keyboard(4, 30))
    assert "splitfull:4:30:best" in callbacks
    assert "splitfull:4:30:audio" in callbacks
    assert "splitrange:4:30:best" in callbacks
    assert "splitrange:4:30:audio" in callbacks


def test_reuse_menu_can_repeat_same_saved_url_for_multiple_operations():
    pytest.importorskip("aiogram")
    from app.bot.advanced_media import reuse_menu_keyboard

    callbacks = _callbacks(reuse_menu_keyboard(31))
    assert "reuse:31:video" in callbacks
    assert "reuse:31:audio" in callbacks
    assert "reuse:31:cut" in callbacks
    assert "reuse:31:split" in callbacks
