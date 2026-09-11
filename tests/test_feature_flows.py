import pytest


class _CommandState:
    def __init__(self) -> None:
        self.state = None
        self.data: dict[str, str] = {}
        self.cleared = False

    async def set_state(self, state) -> None:
        self.state = state

    async def update_data(self, **data) -> None:
        self.data.update(data)

    async def clear(self) -> None:
        self.cleared = True


class _CommandMessage:
    def __init__(self, text: str) -> None:
        self.text = text
        self.answers: list[str] = []

    async def answer(self, text: str) -> None:
        self.answers.append(text)


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


def test_cut_menu_exposes_continuous_split_actions():
    pytest.importorskip("aiogram")
    from app.bot.features import cut_menu_keyboard

    callbacks = _callbacks(cut_menu_keyboard(7))
    assert "advsplit:7:30" in callbacks
    assert "advsplit:7:60" in callbacks


@pytest.mark.asyncio
async def test_cut_command_without_url_waits_for_url(monkeypatch):
    pytest.importorskip("aiogram")
    from app.bot import features

    message = _CommandMessage("/cut")
    state = _CommandState()
    monkeypatch.setattr(features, "_get_message_user", lambda _message: _async_value(object()))

    await features.cut_command(message, state)

    assert state.state == features.FeatureState.waiting_url
    assert state.data == {"intent": "cut"}
    assert message.answers == ["✂️ أرسل رابط الفيديو ثم اختر نوع القص."]


@pytest.mark.asyncio
async def test_mp3_command_with_inline_url_starts_audio_flow(monkeypatch):
    pytest.importorskip("aiogram")
    from app.bot import features

    message = _CommandMessage("/mp3 https://example.com/watch?v=1")
    state = _CommandState()
    processed: list[str] = []
    monkeypatch.setattr(features, "_get_message_user", lambda _message: _async_value(object()))

    async def process(_message, *, intent: str = "general") -> None:
        processed.append(intent)

    monkeypatch.setattr(features, "_process_urls", process)
    await features.mp3_command(message, state)

    assert state.cleared is True
    assert processed == ["audio"]


async def _async_value(value):
    return value
