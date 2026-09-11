from __future__ import annotations

import itertools
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from app.bot import studio
from app.db import (
    MediaAsset,
    MediaProject,
    ProjectAsset,
    SessionLocal,
    StudioAgentMessage,
    User,
    init_db,
)
from app.services.openai_compatible import AIProviderError, ChatReply, ToolChatReply
from app.services.studio_agent import (
    AGENT_TOOLS,
    StudioAgentReply,
    StudioAgentService,
    _parse_json_reply,
)
from app.services.timeline import TimelineService, new_timeline

_IDS = itertools.count(9_200_000)


async def _project() -> tuple[int, int]:
    await init_db()
    async with SessionLocal() as session:
        user = User(telegram_id=next(_IDS))
        session.add(user)
        await session.flush()
        project = MediaProject(
            user_id=user.id,
            chat_id=user.telegram_id,
            width=360,
            height=640,
            fps=24,
            timeline_json=json.dumps(new_timeline(360, 640, 24)),
        )
        session.add(project)
        await session.commit()
        return user.id, project.id


async def _project_with_video(tmp_path) -> tuple[int, int]:
    await init_db()
    media = tmp_path / "agent-video.mp4"
    media.write_bytes(b"fixture")
    async with SessionLocal() as session:
        user = User(telegram_id=next(_IDS))
        session.add(user)
        await session.flush()
        project = MediaProject(
            user_id=user.id,
            chat_id=user.telegram_id,
            width=360,
            height=640,
            fps=24,
            timeline_json=json.dumps(new_timeline(360, 640, 24)),
        )
        asset = MediaAsset(
            user_id=user.id,
            asset_type="video",
            source_type="local",
            local_path=str(media),
            mime_type="video/mp4",
            duration=10,
            width=360,
            height=640,
            file_size=media.stat().st_size,
            metadata_json='{"has_video":true}',
        )
        session.add_all([project, asset])
        await session.flush()
        session.add(
            ProjectAsset(
                project_id=project.id,
                asset_id=asset.id,
                position=0,
                role="main",
            )
        )
        await session.commit()
        return user.id, project.id


class StructuredRegistry:
    def __init__(self, replies: list[str]) -> None:
        self.replies = iter(replies)
        self.calls = 0
        self.messages = []

    async def chat(self, provider_id, model, messages, *, max_reply_chars=None):
        self.calls += 1
        self.messages.append(messages)
        return ChatReply(text=next(self.replies), model=model)


@pytest.mark.asyncio
async def test_agent_structured_json_modifies_same_project_and_persists_context() -> None:
    user_id, project_id = await _project()
    registry = StructuredRegistry(
        [
            '{"message":"تمت إضافة العنوان","tool_calls":['
            '{"name":"add_text","arguments":{"text":"أهلاً","start":0,"duration":3}},'
            '{"name":"set_canvas","arguments":{"preset":"9:16"}}]}'
        ]
    )
    service = StudioAgentService(registry=registry, timelines=TimelineService())
    reply = await service.handle(
        project_id,
        user_id=user_id,
        instruction="أضف كتابة في أول 3 ثوان واجعله Reels",
        provider_id="router",
        model="model",
    )
    assert reply.applied_tools == ("add_text", "set_canvas")
    assert reply.timeline["tracks"][3]["clips"][0]["text"] == "أهلاً"
    contract_prompt = registry.messages[0][-1]["content"]
    assert "Tool contracts" in contract_prompt
    assert '"required":["clip_id","source_end"]' in contract_prompt
    async with SessionLocal() as session:
        count = await session.scalar(
            select(func.count()).select_from(StudioAgentMessage).where(
                StudioAgentMessage.project_id == project_id
            )
        )
        assert count == 2


@pytest.mark.asyncio
async def test_agent_retries_invalid_tool_arguments_once() -> None:
    user_id, project_id = await _project()
    registry = StructuredRegistry(
        [
            '{"message":"","tool_calls":[{"name":"set_canvas","arguments":{"width":13,"height":13}}]}',
            '{"message":"صححت المقاس","tool_calls":[{"name":"set_canvas","arguments":{"preset":"1:1"}}]}',
        ]
    )
    reply = await StudioAgentService(registry=registry, timelines=TimelineService()).handle(
        project_id,
        user_id=user_id,
        instruction="اجعله مربعاً",
        provider_id="router",
        model="model",
    )
    assert registry.calls == 2
    assert reply.timeline["canvas"]["width"] == 1080


@pytest.mark.asyncio
async def test_agent_native_tools_use_same_validated_command_layer() -> None:
    user_id, project_id = await _project()

    class NativeRegistry(StructuredRegistry):
        async def chat_tools(self, provider_id, model, messages, tools):
            assert any(item["function"]["name"] == "set_audio_mode" for item in tools)
            return ToolChatReply(
                text="خفضت الموسيقى",
                model=model,
                tool_calls=(({"name": "set_audio_mode", "arguments": {"mode": "background_music"}}),),
            )

    reply = await StudioAgentService(
        registry=NativeRegistry([]), timelines=TimelineService()
    ).handle(
        project_id,
        user_id=user_id,
        instruction="اجعل الموسيقى بالخلفية",
        provider_id="native",
        model="tool-model",
        native_tools=True,
    )
    assert reply.timeline["options"]["audio_mode"] == "background_music"


def test_agent_tool_schemas_mark_clip_targets_as_required() -> None:
    schemas = {item["function"]["name"]: item["function"]["parameters"] for item in AGENT_TOOLS}
    assert "clip_id" in schemas["trim_clip"]["required"]
    assert "source_end" in schemas["trim_clip"]["required"]
    assert "clip_id" in schemas["add_transition"]["required"]
    assert "text" in schemas["add_text"]["required"]


@pytest.mark.asyncio
async def test_agent_safely_resolves_missing_clip_id_when_only_one_clip_exists(
    tmp_path,
) -> None:
    user_id, project_id = await _project_with_video(tmp_path)

    class NativeRegistry(StructuredRegistry):
        async def chat_tools(self, provider_id, model, messages, tools):
            return ToolChatReply(
                text="قصصت البداية",
                model=model,
                tool_calls=(
                    {
                        "name": "trim_clip",
                        "arguments": {"source_start": 1, "source_end": 8},
                    },
                ),
            )

    reply = await StudioAgentService(
        registry=NativeRegistry([]), timelines=TimelineService()
    ).handle(
        project_id,
        user_id=user_id,
        instruction="قص ثانية من البداية",
        provider_id="native",
        model="tool-model",
        native_tools=True,
    )

    visual = reply.timeline["tracks"][0]["clips"]
    assert visual[0]["source_start"] == 1
    assert visual[0]["duration"] == 7


def test_agent_rejects_shell_and_unknown_tools_before_dispatch() -> None:
    with pytest.raises(ValueError, match="invalid tool"):
        _parse_json_reply(
            '{"message":"","tool_calls":[{"name":"shell","arguments":{"cmd":"ffmpeg"}}]}'
        )


@pytest.mark.asyncio
async def test_agent_stops_after_bounded_invalid_json_retry() -> None:
    user_id, project_id = await _project()
    registry = StructuredRegistry(["not json", '{"message":"still invalid"}'])
    with pytest.raises(AIProviderError, match="invalid editing JSON"):
        await StudioAgentService(registry=registry, timelines=TimelineService()).handle(
            project_id,
            user_id=user_id,
            instruction="do something",
            provider_id="router",
            model="model",
        )
    assert registry.calls == 2


@pytest.mark.asyncio
async def test_mocked_telegram_agent_instruction_updates_timeline_and_starts_preview(
    monkeypatch,
) -> None:
    edits: list[str] = []
    renders: list[tuple[int, int, str]] = []

    class Thinking:
        async def edit_text(self, text, **kwargs):
            edits.append(text)

    class Message:
        text = "قص البداية ثم اعرض معاينة"
        from_user = SimpleNamespace(id=123, username="agent")
        chat = SimpleNamespace(id=456)
        bot = SimpleNamespace()

        async def answer(self, text, **kwargs):
            return Thinking()

    class State:
        async def get_data(self):
            return {
                "studio_project_id": 77,
                "studio_agent_provider_id": "router",
                "studio_agent_model": "model",
                "studio_agent_native_tools": False,
            }

        async def clear(self):
            raise AssertionError("valid project must keep its agent state")

    class Agent:
        async def handle(self, *args, **kwargs):
            return StudioAgentReply(
                text="تم القص",
                model="model",
                applied_tools=("trim_clip", "render_preview"),
                render_action="preview",
                timeline={"version": 2},
            )

    async def user(_message):
        return SimpleNamespace(id=9)

    async def project(project_id, *, user_id):
        return SimpleNamespace(id=project_id, user_id=user_id)

    async def enqueue(_message, project_id, user_id, kind):
        renders.append((project_id, user_id, kind))

    monkeypatch.setattr(studio, "_message_user", user)
    monkeypatch.setattr(studio.project_service, "get_project", project)
    monkeypatch.setattr(studio, "_enqueue_agent_render", enqueue)
    await studio.handle_agent_instruction(Message(), State(), agent=Agent())
    assert any("تم القص" in text for text in edits)
    assert renders == [(77, 9, "preview")]


@pytest.mark.asyncio
async def test_agent_mode_routes_url_only_message_to_existing_downloader_flow(monkeypatch) -> None:
    routed: list[str] = []

    class Message:
        text = "https://youtu.be/example"
        from_user = SimpleNamespace(id=123, username="agent")

    class State:
        async def get_data(self):
            return {"studio_project_id": 88}

        async def clear(self):
            return None

    async def user(_message):
        return SimpleNamespace(id=10)

    async def project(project_id, *, user_id):
        return SimpleNamespace(id=project_id, user_id=user_id)

    async def route(message, state):
        routed.append(message.text)

    monkeypatch.setattr(studio, "_message_user", user)
    monkeypatch.setattr(studio.project_service, "get_project", project)
    monkeypatch.setattr(studio, "receive_project_url", route)
    await studio.handle_agent_instruction(Message(), State())
    assert routed == ["https://youtu.be/example"]
