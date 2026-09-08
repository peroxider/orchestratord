"""ChatMessageBridge contract tests (``docs/FEATURE_GAP_VS_MULTICA.md`` §6.1d).

Pins the fold: the bridge consumes the BackendRunner's synchronous
``progress_reporter`` callbacks and lands one ``role="assistant"`` message
per turn on the chat timeline (``messages`` table), with ``MessageRepository``
auto-assigning per-session ``seq``. Tool callbacks are ignored; ``on_error``
flushes the partial turn; ``flush()`` closes any turn left open.

Runs against the live ``orchestratord_test`` database (reuses the ``tests/api``
fixtures) and skips when Postgres is unreachable.

Reference: docs/FEATURE_GAP_VS_MULTICA.md §6.1d.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from orchestratord.api.realtime import get_broker, reset_broker
from orchestratord.chat_bridge import ChatMessageBridge, is_progress_reporter
from orchestratord.db import models as orm
from orchestratord.db.engine import build_session_factory
from orchestratord.db.repository import Repositories

pytestmark = pytest.mark.database


async def _seed_session(db, **overrides) -> orm.Session:
    defaults = {
        "id": uuid4(),
        "workspace_id": uuid4(),
        "issue_id": None,
        "agent_id": None,
        "run_id": None,
        "mode": "single",
        "status": "running",
    }
    defaults.update(overrides)
    session = orm.Session(created_at=datetime.now(UTC), **defaults)
    await Repositories(db).sessions.add(session)
    await db.commit()
    return session


async def _list_messages(client, session_id) -> list[dict]:
    resp = await client.get(f"/api/sessions/{session_id}/messages")
    assert resp.status_code == 200
    return resp.json()["messages"]


class TestTurnFolding:
    async def test_deltas_fold_into_one_assistant_message(
        self, client, db, db_engine
    ) -> None:
        session = await _seed_session(db)
        factory = build_session_factory(db_engine)
        bridge = ChatMessageBridge(factory, session.id, session.workspace_id)

        bridge.on_text_delta("Hello, ")
        bridge.on_text_delta("world")
        bridge.on_text_delta("!")
        bridge.on_turn_complete(object(), object())
        await bridge.flush()

        messages = await _list_messages(client, session.id)
        assert len(messages) == 1
        assert messages[0]["role"] == "assistant"
        assert messages[0]["content"] == "Hello, world!"
        assert messages[0]["seq"] == 0

    async def test_multiple_turns_produce_sequential_messages(
        self, client, db, db_engine
    ) -> None:
        session = await _seed_session(db)
        factory = build_session_factory(db_engine)
        bridge = ChatMessageBridge(factory, session.id, session.workspace_id)

        bridge.on_text_delta("first turn")
        bridge.on_turn_complete(object(), object())
        await bridge.flush()

        bridge.on_text_delta("second turn")
        bridge.on_session_complete(object(), object())
        await bridge.flush()

        messages = await _list_messages(client, session.id)
        assert len(messages) == 2
        assert [m["seq"] for m in messages] == [0, 1]
        assert messages[0]["content"] == "first turn"
        assert messages[1]["content"] == "second turn"

    async def test_empty_turn_produces_no_message(
        self, client, db, db_engine
    ) -> None:
        session = await _seed_session(db)
        factory = build_session_factory(db_engine)
        bridge = ChatMessageBridge(factory, session.id, session.workspace_id)

        bridge.on_turn_complete(object(), object())
        await bridge.flush()

        messages = await _list_messages(client, session.id)
        assert messages == []

    async def test_tool_callbacks_are_ignored(
        self, client, db, db_engine
    ) -> None:
        session = await _seed_session(db)
        factory = build_session_factory(db_engine)
        bridge = ChatMessageBridge(factory, session.id, session.workspace_id)

        bridge.on_tool_call("run", "call-1")
        bridge.on_tool_result("call-1")
        await bridge.flush()

        messages = await _list_messages(client, session.id)
        assert messages == []


class TestFailurePaths:
    async def test_on_error_flushes_partial_turn(
        self, client, db, db_engine
    ) -> None:
        session = await _seed_session(db)
        factory = build_session_factory(db_engine)
        bridge = ChatMessageBridge(factory, session.id, session.workspace_id)

        bridge.on_text_delta("partial answer before crash")
        bridge.on_error("backend died")
        await bridge.flush()

        messages = await _list_messages(client, session.id)
        assert len(messages) == 1
        assert messages[0]["content"] == "partial answer before crash"

    async def test_flush_closes_unclosed_turn(
        self, client, db, db_engine
    ) -> None:
        session = await _seed_session(db)
        factory = build_session_factory(db_engine)
        bridge = ChatMessageBridge(factory, session.id, session.workspace_id)

        bridge.on_text_delta("never saw turn_complete")
        await bridge.flush()

        messages = await _list_messages(client, session.id)
        assert len(messages) == 1
        assert messages[0]["content"] == "never saw turn_complete"

    async def test_agent_id_recorded_on_assistant_messages(
        self, client, db, db_engine
    ) -> None:
        session = await _seed_session(db)
        agent_id = uuid4()
        factory = build_session_factory(db_engine)
        bridge = ChatMessageBridge(
            factory, session.id, session.workspace_id, agent_id=agent_id
        )

        bridge.on_text_delta("from the agent")
        bridge.on_turn_complete(object(), object())
        await bridge.flush()

        messages = await _list_messages(client, session.id)
        assert len(messages) == 1
        assert messages[0]["agent_id"] == str(agent_id)


class TestRealtimePublishing:
    async def test_delta_then_turn_complete_frames(
        self, client, db, db_engine
    ) -> None:
        reset_broker()
        session = await _seed_session(db)
        factory = build_session_factory(db_engine)
        bridge = ChatMessageBridge(factory, session.id, session.workspace_id)
        topic = f"chat.{session.id}"
        _sub_id, frames = await get_broker().subscribe({topic})

        bridge.on_text_delta("stream me")
        bridge.on_turn_complete(object(), object())
        await bridge.flush()

        first = await asyncio.wait_for(anext(frames), timeout=2)
        assert first == {
            "topic": topic,
            "payload": {
                "event": "text_delta",
                "session_id": str(session.id),
                "text": "stream me",
            },
        }
        second = await asyncio.wait_for(anext(frames), timeout=2)
        assert second["payload"]["event"] == "turn_complete"
        reset_broker()

    async def test_tool_call_frame_published(self, client, db, db_engine) -> None:
        reset_broker()
        session = await _seed_session(db)
        factory = build_session_factory(db_engine)
        bridge = ChatMessageBridge(factory, session.id, session.workspace_id)
        _sub_id, frames = await get_broker().subscribe(
            {f"chat.{session.id}"}
        )

        bridge.on_tool_call("run", "call-9")

        frame = await asyncio.wait_for(anext(frames), timeout=2)
        assert frame["payload"] == {
            "event": "tool_call",
            "session_id": str(session.id),
            "tool_name": "run",
            "call_id": "call-9",
        }
        reset_broker()

    async def test_error_frame_and_partial_text_persisted(
        self, client, db, db_engine
    ) -> None:
        reset_broker()
        session = await _seed_session(db)
        factory = build_session_factory(db_engine)
        bridge = ChatMessageBridge(factory, session.id, session.workspace_id)
        _sub_id, frames = await get_broker().subscribe(
            {f"chat.{session.id}"}
        )

        bridge.on_text_delta("partial before crash")
        bridge.on_error("backend died")
        await bridge.flush()

        delta = await asyncio.wait_for(anext(frames), timeout=2)
        assert delta["payload"]["event"] == "text_delta"
        error = await asyncio.wait_for(anext(frames), timeout=2)
        assert error["payload"]["event"] == "error"
        assert error["payload"]["message"] == "backend died"

        messages = await _list_messages(client, session.id)
        assert len(messages) == 1
        assert messages[0]["content"] == "partial before crash"
        reset_broker()


class TestDuckTyping:
    def test_bridge_satisfies_progress_reporter_protocol(self) -> None:
        assert is_progress_reporter(ChatMessageBridge(None, uuid4(), uuid4()))

    def test_arbitrary_object_does_not(self) -> None:
        assert not is_progress_reporter(object())
