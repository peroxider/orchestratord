"""Chat message endpoints for sessions (``docs/FEATURE_GAP_VS_MULTICA.md`` §6.1).

Pins the chat-timeline surface: ``GET /api/sessions/{id}/messages`` returns
the per-session message list in ``seq`` order, and ``POST
/api/sessions/{id}/messages`` appends a message with an auto-assigned
``seq``. The repository (``MessageRepository.append``) handles
``COALESCE(MAX(seq), -1) + 1`` so concurrent appends from the runner and
user posts don't collide.

Sessions have no HTTP create endpoint, so these tests seed ``Session`` rows
directly through the ``db`` fixture and drive the new endpoints over HTTP.
Runs against the live ``orchestratord_test`` database and skips when
Postgres is unreachable.

Reference: docs/FEATURE_GAP_VS_MULTICA.md §6.1.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from uuid import uuid4

import pytest

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


async def _seed_workspace(db, **overrides) -> orm.Workspace:
    defaults = {
        "id": uuid4(),
        "slug": f"ws-{uuid4().hex[:8]}",
        "name": "Test Workspace",
        "created_at": datetime.now(UTC),
    }
    defaults.update(overrides)
    ws = orm.Workspace(**defaults)
    await Repositories(db).workspaces.add(ws)
    await db.commit()
    return ws


async def _seed_agent(db, workspace_id, **overrides) -> orm.Agent:
    defaults = {
        "id": uuid4(),
        "workspace_id": workspace_id,
        "name": "test-agent",
        "provider": "test",
        "runtime_id": uuid4(),
        "capabilities_cache_jsonb": {},
        "created_at": datetime.now(UTC),
    }
    defaults.update(overrides)
    agent = orm.Agent(**defaults)
    await Repositories(db).agents.add(agent)
    await db.commit()
    return agent


async def _post_message(client, session_id, *, role="user", content="hello",
                       agent_id=None, author_label="me") -> "Response":
    payload: dict = {"role": role, "content": content}
    if agent_id is not None:
        payload["agent_id"] = str(agent_id)
    if author_label is not None:
        payload["author_label"] = author_label
    return await client.post(
        f"/api/sessions/{session_id}/messages", json=payload
    )


class TestListMessages:
    async def test_list_empty(self, client, db) -> None:
        session = await _seed_session(db)
        resp = await client.get(f"/api/sessions/{session.id}/messages")
        assert resp.status_code == 200
        data = resp.json()
        assert data["session_id"] == str(session.id)
        assert data["workspace_id"] == str(session.workspace_id)
        assert data["messages"] == []

    async def test_list_returns_messages_in_seq_order(self, client, db) -> None:
        session = await _seed_session(db)
        for body in ["first", "second", "third"]:
            resp = await _post_message(client, session.id, content=body)
            assert resp.status_code == 201
        resp = await client.get(f"/api/sessions/{session.id}/messages")
        assert resp.status_code == 200
        seqs = [m["seq"] for m in resp.json()["messages"]]
        contents = [m["content"] for m in resp.json()["messages"]]
        assert seqs == sorted(seqs)
        assert contents == ["first", "second", "third"]

    async def test_list_404_when_session_missing(self, client, db) -> None:
        resp = await client.get(f"/api/sessions/{uuid4()}/messages")
        assert resp.status_code == 404

    async def test_list_after_seq_filters_tail(self, client, db) -> None:
        session = await _seed_session(db)
        for body in ["a", "b", "c", "d"]:
            await _post_message(client, session.id, content=body)
        resp = await client.get(
            f"/api/sessions/{session.id}/messages?after_seq=1"
        )
        assert resp.status_code == 200
        msgs = resp.json()["messages"]
        assert [m["seq"] for m in msgs] == [2, 3]
        assert [m["content"] for m in msgs] == ["c", "d"]


class TestPostMessage:
    async def test_post_auto_assigns_seq(self, client, db) -> None:
        session = await _seed_session(db)
        resp = await _post_message(client, session.id, content="hi")
        assert resp.status_code == 201
        body = resp.json()
        assert body["seq"] == 0
        assert body["role"] == "user"
        assert body["content"] == "hi"
        assert body["author_label"] == "me"
        assert body["agent_id"] is None

    async def test_post_multiple_assigns_sequential_seqs(self, client, db) -> None:
        session = await _seed_session(db)
        seqs = []
        for body in ["a", "b", "c"]:
            resp = await _post_message(client, session.id, content=body)
            assert resp.status_code == 201
            seqs.append(resp.json()["seq"])
        assert seqs == [0, 1, 2]

    async def test_post_rejects_invalid_role(self, client, db) -> None:
        session = await _seed_session(db)
        resp = await client.post(
            f"/api/sessions/{session.id}/messages",
            json={"role": "narrator", "content": "hi"},
        )
        assert resp.status_code == 422

    async def test_post_rejects_empty_content(self, client, db) -> None:
        session = await _seed_session(db)
        resp = await client.post(
            f"/api/sessions/{session.id}/messages",
            json={"role": "user", "content": ""},
        )
        assert resp.status_code == 422

    async def test_post_404_when_session_missing(self, client, db) -> None:
        resp = await _post_message(client, uuid4())
        assert resp.status_code == 404

    async def test_post_assistant_message_with_agent(self, client, db) -> None:
        session = await _seed_session(db)
        agent_id = uuid4()
        resp = await _post_message(
            client,
            session.id,
            role="assistant",
            content="reply from agent",
            agent_id=agent_id,
            author_label=None,
        )
        assert resp.status_code == 201
        body = resp.json()
        assert body["role"] == "assistant"
        assert body["agent_id"] == str(agent_id)
        assert body["author_label"] is None

    async def test_post_then_list_roundtrip(self, client, db) -> None:
        session = await _seed_session(db)
        for content in ["hello", "world"]:
            await _post_message(client, session.id, content=content)
        resp = await client.get(f"/api/sessions/{session.id}/messages")
        assert resp.status_code == 200
        msgs = resp.json()["messages"]
        assert len(msgs) == 2
        assert msgs[0]["content"] == "hello"
        assert msgs[1]["content"] == "world"


class TestStartChatSession:
    async def test_start_creates_session_and_initial_message(
        self, client, db
    ) -> None:
        ws = await _seed_workspace(db)
        resp = await client.post(
            f"/api/workspaces/{ws.id}/chat/sessions",
            json={"prompt": "tell me about orchestratord"},
        )
        assert resp.status_code == 201
        body = resp.json()
        assert body["workspace_id"] == str(ws.id)
        assert body["status"] == "pending"
        msg = body["message"]
        assert msg["role"] == "user"
        assert msg["content"] == "tell me about orchestratord"
        assert msg["author_label"] == "me"
        assert msg["agent_id"] is None

    async def test_start_with_agent_id(self, client, db) -> None:
        ws = await _seed_workspace(db)
        agent = await _seed_agent(db, ws.id, name="claude")
        resp = await client.post(
            f"/api/workspaces/{ws.id}/chat/sessions",
            json={"prompt": "hi", "agent_id": str(agent.id)},
        )
        assert resp.status_code == 201
        body = resp.json()
        # Verify the session was created with the agent
        list_resp = await client.get(
            f"/api/sessions/{body['session_id']}"
        )
        assert list_resp.status_code == 200
        assert list_resp.json()["agent_id"] == str(agent.id)

    async def test_start_404_when_workspace_missing(self, client, db) -> None:
        resp = await client.post(
            f"/api/workspaces/{uuid4()}/chat/sessions",
            json={"prompt": "hi"},
        )
        assert resp.status_code == 404

    async def test_start_404_when_agent_not_in_workspace(
        self, client, db
    ) -> None:
        ws = await _seed_workspace(db)
        # Agent exists but in a different workspace
        other_ws = await _seed_workspace(db)
        agent = await _seed_agent(db, other_ws.id)
        resp = await client.post(
            f"/api/workspaces/{ws.id}/chat/sessions",
            json={"prompt": "hi", "agent_id": str(agent.id)},
        )
        assert resp.status_code == 404

    async def test_start_rejects_empty_prompt(self, client, db) -> None:
        ws = await _seed_workspace(db)
        resp = await client.post(
            f"/api/workspaces/{ws.id}/chat/sessions",
            json={"prompt": ""},
        )
        assert resp.status_code == 422

    async def test_start_session_appears_in_messages_list(
        self, client, db
    ) -> None:
        ws = await _seed_workspace(db)
        resp = await client.post(
            f"/api/workspaces/{ws.id}/chat/sessions",
            json={"prompt": "first prompt"},
        )
        assert resp.status_code == 201
        session_id = resp.json()["session_id"]
        list_resp = await client.get(f"/api/sessions/{session_id}/messages")
        assert list_resp.status_code == 200
        msgs = list_resp.json()["messages"]
        assert len(msgs) == 1
        assert msgs[0]["content"] == "first prompt"


class TestConcurrentAppend:
    """Regression for the verifier-found seq race (§6.1a).

    Before the parent-row ``FOR UPDATE`` lock plus the
    ``uq_messages_session_seq`` constraint, concurrent appends all read the
    same ``MAX(seq)`` and committed duplicate seqs with 201s — silently
    corrupting the ``after_seq`` tail-fetch contract.
    """

    async def test_parallel_repository_appends_get_unique_seqs(
        self, db, client, db_engine
    ) -> None:
        session_row = await _seed_session(db)
        factory = build_session_factory(db_engine)

        async def append_one(i: int) -> int:
            async with factory() as db2:
                msg = orm.Message(
                    id=uuid4(),
                    session_id=session_row.id,
                    workspace_id=session_row.workspace_id,
                    seq=0,
                    role="user",
                    content=f"m{i}",
                    agent_id=None,
                    author_label=None,
                    created_at=datetime.now(UTC),
                )
                await Repositories(db2).messages.append(msg)
                await db2.commit()
                return msg.seq

        seqs = await asyncio.gather(*(append_one(i) for i in range(12)))
        assert sorted(seqs) == list(range(12))

    async def test_parallel_http_posts_get_unique_seqs(
        self, client, db
    ) -> None:
        session_row = await _seed_session(db)

        async def post_one(i: int) -> int:
            resp = await client.post(
                f"/api/sessions/{session_row.id}/messages",
                json={"role": "user", "content": f"m{i}"},
            )
            assert resp.status_code == 201
            return resp.json()["seq"]

        seqs = await asyncio.gather(*(post_one(i) for i in range(12)))
        assert sorted(seqs) == list(range(12))
