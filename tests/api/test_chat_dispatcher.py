"""ChatDispatcher contract tests (``docs/FEATURE_GAP_VS_MULTICA.md`` §6.1d).

Pins the claim/dispatch loop: pending sessions (created by chat-start §6.1c
and mention §6.2) are claimed atomically (``FOR UPDATE SKIP LOCKED``) oldest
first, flipped to ``running``, driven through a ``runner_invoke`` seam with a
``ChatMessageBridge`` attached, and land on a terminal status
(``completed`` / ``failed``) — with the no-resurrect guard keeping an
externally-stopped session stopped.

Runs against the live ``orchestratord_test`` database (reuses the ``tests/api``
fixtures) and skips when Postgres is unreachable.

Reference: docs/FEATURE_GAP_VS_MULTICA.md §6.1d.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from orchestratord.chat_bridge import ChatMessageBridge
from orchestratord.chat_dispatcher import ChatDispatcher, initial_message_of
from orchestratord.db import models as orm
from orchestratord.db.engine import build_session_factory
from orchestratord.db.repository import Repositories

pytestmark = pytest.mark.database


async def _seed_session(db, **overrides) -> orm.Session:
    created_at = overrides.pop("created_at", datetime.now(UTC))
    defaults = {
        "id": uuid4(),
        "workspace_id": uuid4(),
        "issue_id": None,
        "agent_id": None,
        "run_id": None,
        "mode": "single",
        "status": "pending",
    }
    defaults.update(overrides)
    session = orm.Session(created_at=created_at, **defaults)
    await Repositories(db).sessions.add(session)
    await db.commit()
    return session


async def _seed_message(db, session: orm.Session, role: str, content: str) -> None:
    await Repositories(db).messages.append(
        orm.Message(
            id=uuid4(),
            session_id=session.id,
            workspace_id=session.workspace_id,
            seq=0,  # sentinel — repository auto-assigns
            role=role,
            content=content,
            agent_id=None,
            author_label="me" if role == "user" else None,
            created_at=datetime.now(UTC),
        )
    )
    await db.commit()


async def _get_status(factory, session_id) -> str:
    async with factory() as db:
        row = await Repositories(db).sessions.get(session_id)
        assert row is not None
        return row.status


async def _list_messages(factory, session_id) -> list[orm.Message]:
    async with factory() as db:
        return list(await Repositories(db).messages.list_for_session(session_id))


def _dispatcher(engine) -> tuple[ChatDispatcher, list[dict]]:
    """Dispatcher wired to a recording fake ``runner_invoke``."""
    calls: list[dict] = []

    async def fake_runner(session, prompt, bridge: ChatMessageBridge) -> None:
        calls.append({"session": session, "prompt": prompt, "bridge": bridge})
        bridge.on_text(f"echo: {prompt}")
        bridge.on_turn_complete(object(), object())

    return ChatDispatcher(build_session_factory(engine), fake_runner), calls


class TestClaiming:
    async def test_claim_flips_pending_to_running(self, db, db_engine) -> None:
        session = await _seed_session(db)
        dispatcher, _ = _dispatcher(db_engine)

        claimed = await dispatcher.claim_next()

        assert claimed is not None
        assert claimed.id == session.id
        assert claimed.status == "running"
        assert await _get_status(build_session_factory(db_engine), session.id) == (
            "running"
        )

    async def test_claim_empty_queue_returns_none(self, db_engine) -> None:
        dispatcher, _ = _dispatcher(db_engine)
        assert await dispatcher.claim_next() is None

    async def test_claim_oldest_first(self, db, db_engine) -> None:
        older = await _seed_session(
            db, created_at=datetime.now(UTC) - timedelta(seconds=10)
        )
        newer = await _seed_session(db)
        dispatcher, _ = _dispatcher(db_engine)

        first = await dispatcher.claim_next()
        second = await dispatcher.claim_next()

        assert first is not None and first.id == older.id
        assert second is not None and second.id == newer.id

    async def test_claimed_session_not_claimed_twice(self, db, db_engine) -> None:
        await _seed_session(db)
        dispatcher, _ = _dispatcher(db_engine)

        first = await dispatcher.claim_next()
        second = await dispatcher.claim_next()

        assert first is not None
        assert second is None


class TestDispatch:
    async def test_happy_path_lands_message_and_completes(
        self, db, db_engine
    ) -> None:
        session = await _seed_session(db)
        await _seed_message(db, session, "user", "summarize the diff")
        dispatcher, calls = _dispatcher(db_engine)

        claimed = await dispatcher.claim_next()
        assert claimed is not None
        terminal = await dispatcher.dispatch_one(claimed)

        assert terminal == "completed"
        assert len(calls) == 1
        assert calls[0]["prompt"] == "summarize the diff"
        assert await _get_status(build_session_factory(db_engine), session.id) == (
            "completed"
        )
        messages = await _list_messages(build_session_factory(db_engine), session.id)
        assert [m.content for m in messages] == [
            "summarize the diff",
            "echo: summarize the diff",
        ]
        assert messages[-1].role == "assistant"

    async def test_failure_flips_to_failed_and_keeps_partial_answer(
        self, db, db_engine
    ) -> None:
        session = await _seed_session(db)
        await _seed_message(db, session, "user", "doomed prompt")
        factory = build_session_factory(db_engine)

        async def failing_runner(session, prompt, bridge) -> None:
            bridge.on_text("partial answer before crash")
            raise RuntimeError("backend died")

        dispatcher = ChatDispatcher(factory, failing_runner)
        claimed = await dispatcher.claim_next()
        assert claimed is not None

        terminal = await dispatcher.dispatch_one(claimed)

        assert terminal == "failed"
        assert await _get_status(factory, session.id) == "failed"
        messages = await _list_messages(factory, session.id)
        assert messages[-1].content == "partial answer before crash"
        assert messages[-1].role == "assistant"

    async def test_no_prompt_dispatches_with_empty_prompt(self, db, db_engine) -> None:
        session = await _seed_session(db)
        dispatcher, calls = _dispatcher(db_engine)

        claimed = await dispatcher.claim_next()
        assert claimed is not None
        await dispatcher.dispatch_one(claimed)

        assert calls[0]["prompt"] == ""

    async def test_externally_stopped_session_not_resurrected(
        self, db, db_engine
    ) -> None:
        session = await _seed_session(db)
        factory = build_session_factory(db_engine)
        dispatcher, _ = _dispatcher(db_engine)

        claimed = await dispatcher.claim_next()
        assert claimed is not None

        # §5.2.3 stop lands between claim and terminal write.
        async with factory() as db2:
            row = await Repositories(db2).sessions.get(session.id)
            assert row is not None
            row.status = "stopped"
            await db2.commit()

        terminal = await dispatcher.dispatch_one(claimed)

        assert terminal == "completed"
        assert await _get_status(factory, session.id) == "stopped"


class TestInitialMessageOf:
    def test_returns_first_user_message(self) -> None:
        first = orm.Message(
            id=uuid4(),
            session_id=uuid4(),
            workspace_id=uuid4(),
            seq=0,
            role="system",
            content="boot",
            agent_id=None,
            author_label=None,
            created_at=datetime.now(UTC),
        )
        second = orm.Message(
            id=uuid4(),
            session_id=first.session_id,
            workspace_id=first.workspace_id,
            seq=1,
            role="user",
            content="the prompt",
            agent_id=None,
            author_label="me",
            created_at=datetime.now(UTC),
        )
        assert initial_message_of([first, second]) is second

    def test_none_when_no_user_message(self) -> None:
        system = orm.Message(
            id=uuid4(),
            session_id=uuid4(),
            workspace_id=uuid4(),
            seq=0,
            role="system",
            content="boot",
            agent_id=None,
            author_label=None,
            created_at=datetime.now(UTC),
        )
        assert initial_message_of([system]) is None

    def test_none_when_empty(self) -> None:
        assert initial_message_of([]) is None
