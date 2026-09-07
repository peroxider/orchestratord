"""Session-control live-wiring tests (``docs/FEATURE_GAP_VS_MULTICA.md`` §5.2.3).

Pins the Phase A.2 contract: when a :class:`BackendRunner` is wired into
the FastAPI app (via :func:`orchestratord.api.runtime.set_backend_runner`),
the approve/deny/pause/resume/stop endpoints forward the operator
decision to the live :class:`AgentSession` registered in
``runner.registry``. Capability bits from the 8-bit matrix gate the
operations; lifecycle changes fan out via the realtime broker.

The Phase-1 contract (``tests/api/test_sessions_api.py``) covers the
DB-only behaviour when no runner is wired; this module covers the
runner-aware slice added in Phase A.2.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest

from orchestratord.api.realtime import get_broker, reset_broker
from orchestratord.api.runtime import (
    reset_backend_runner,
    set_backend_runner,
)
from orchestratord.backend_runner import BackendRunner
from orchestratord.db import models as orm
from orchestratord.db.repository import Repositories
from orchestratord.runtime import LiveSession, LiveSessionRegistry
from orchestratord.spi.approval import ApprovalDecision
from orchestratord.spi.capabilities import BackendCapabilities
from orchestratord.spi.events import EventEnvelope, EventKind

pytestmark = pytest.mark.database


# ----------------------------------------------------------------------
# Test doubles — minimal stubs that record calls so tests can assert on
# the wire contract without booting real backends.
# ----------------------------------------------------------------------


class _FakeAgentSession:
    """Records approve/interrupt calls for assertion."""

    def __init__(self) -> None:
        self.approve_calls: list[tuple[str, ApprovalDecision]] = []
        self.interrupt_calls = 0
        self.session_id: str = "fake"
        self.conversation_id: str | None = None
        self.capabilities: BackendCapabilities = BackendCapabilities()

    async def approve(self, request_id: str, decision: ApprovalDecision) -> None:
        self.approve_calls.append((request_id, decision))

    async def interrupt(self) -> None:
        self.interrupt_calls += 1

    async def send(self, content: Any) -> None:  # pragma: no cover
        return None

    def events(self):  # pragma: no cover — not exercised here
        async def _gen():
            return
            yield EventEnvelope(kind=EventKind.TEXT_DELTA, payload={})

        return _gen()


@dataclass
class _FakeProcessTree:
    """Records pause/resume/kill for assertion (no real psutil)."""

    pause_calls: int = 0
    resume_calls: int = 0
    kill_calls: int = 0

    def pause(self) -> None:
        self.pause_calls += 1

    def resume(self) -> None:
        self.resume_calls += 1

    def kill(self) -> None:
        self.kill_calls += 1


class _FakeBackendRunner(BackendRunner):
    """BackendRunner subclass that exposes a clean registry for tests.

    Skips ``BackendRunner.__init__`` — we do not need approval policy or
    task registry for the live-control tests; the router only reads
    ``runner.registry``.
    """

    def __init__(self) -> None:  # type: ignore[no-super-call]
        self.registry = LiveSessionRegistry()


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
    return session


async def _seed_event(db, session, seq, kind=EventKind.APPROVAL_REQUEST, **payload) -> None:
    event = orm.Event(
        id=uuid4(),
        session_id=session.id,
        sequence=seq,
        kind=kind.value,
        payload=payload,
        run_id=None,
        issue_id=None,
        workspace_id=session.workspace_id,
        created_at=datetime.now(UTC),
    )
    await Repositories(db).events.append(event)


# ----------------------------------------------------------------------
# Fixtures — override the production ``get_backend_runner`` accessor so
# each test sees a fresh ``_FakeBackendRunner`` with a clean registry.
# ----------------------------------------------------------------------


@pytest.fixture
def fake_runner() -> _FakeBackendRunner:
    reset_backend_runner()
    reset_broker()
    runner = _FakeBackendRunner()
    set_backend_runner(runner)
    yield runner
    reset_backend_runner()
    reset_broker()


# ----------------------------------------------------------------------
# Tests
# ----------------------------------------------------------------------


class TestApproveForwardsToLiveSession:
    """When a live session is registered and supports approval_hooks,
    POST /approve must reach ``spi_session.approve(request_id, ALLOW)``."""

    async def test_approve_with_approval_hooks_forwards_decision(
        self, client, db, fake_runner,
    ) -> None:
        session = await _seed_session(db)
        await _seed_event(db, session, 1, request_id="req-1")
        await db.commit()

        spi = _FakeAgentSession()
        spi.capabilities = BackendCapabilities(approval_hooks=True)
        await fake_runner.registry.register(
            str(session.id),
            LiveSession(spi_session=spi, capabilities=spi.capabilities),
        )

        resp = await client.post(
            f"/api/sessions/{session.id}/approve",
            json={"request_id": "req-1"},
        )
        assert resp.status_code == 200
        assert resp.json()["decision"] == "approved"
        assert spi.approve_calls == [("req-1", ApprovalDecision.ALLOW)]

    async def test_deny_with_approval_hooks_forwards_decision(
        self, client, db, fake_runner,
    ) -> None:
        session = await _seed_session(db)
        await _seed_event(db, session, 1, request_id="req-2")
        await db.commit()

        spi = _FakeAgentSession()
        spi.capabilities = BackendCapabilities(approval_hooks=True)
        await fake_runner.registry.register(
            str(session.id),
            LiveSession(spi_session=spi, capabilities=spi.capabilities),
        )

        resp = await client.post(
            f"/api/sessions/{session.id}/deny",
            json={"request_id": "req-2"},
        )
        assert resp.status_code == 200
        assert resp.json()["decision"] == "denied"
        assert spi.approve_calls == [("req-2", ApprovalDecision.DENY)]


class TestApproveCapabilityGate:
    """A live session without ``approval_hooks`` returns 409 — the
    decision is recorded in the DB but cannot be delivered to the
    backend, so the caller must be told."""

    async def test_approve_without_approval_hooks_returns_409(
        self, client, db, fake_runner,
    ) -> None:
        session = await _seed_session(db)
        await _seed_event(db, session, 1, request_id="req-3")
        await db.commit()

        spi = _FakeAgentSession()
        # approval_hooks defaults to False
        await fake_runner.registry.register(
            str(session.id),
            LiveSession(spi_session=spi, capabilities=spi.capabilities),
        )

        resp = await client.post(
            f"/api/sessions/{session.id}/approve",
            json={"request_id": "req-3"},
        )
        assert resp.status_code == 409
        assert "approval_hooks" in resp.json()["detail"]
        assert spi.approve_calls == []


class TestStopForwardsToLiveSession:
    async def test_stop_with_interrupt_calls_spi_session_interrupt(
        self, client, db, fake_runner,
    ) -> None:
        session = await _seed_session(db, status="running")
        await db.commit()

        spi = _FakeAgentSession()
        spi.capabilities = BackendCapabilities(interrupt=True)
        await fake_runner.registry.register(
            str(session.id),
            LiveSession(spi_session=spi, capabilities=spi.capabilities),
        )

        resp = await client.post(f"/api/sessions/{session.id}/stop")
        assert resp.status_code == 200
        assert resp.json()["status"] == "stopped"
        assert spi.interrupt_calls == 1

    async def test_stop_with_interrupt_and_process_tree_calls_both(
        self, client, db, fake_runner,
    ) -> None:
        session = await _seed_session(db, status="running")
        await db.commit()

        spi = _FakeAgentSession()
        spi.capabilities = BackendCapabilities(interrupt=True)
        proc = _FakeProcessTree()
        await fake_runner.registry.register(
            str(session.id),
            LiveSession(
                spi_session=spi,
                capabilities=spi.capabilities,
                process_tree=proc,
            ),
        )

        resp = await client.post(f"/api/sessions/{session.id}/stop")
        assert resp.status_code == 200
        assert spi.interrupt_calls == 1
        assert proc.kill_calls == 1


class TestStopCapabilityGate:
    async def test_stop_without_interrupt_and_no_process_tree_returns_409(
        self, client, db, fake_runner,
    ) -> None:
        session = await _seed_session(db, status="running")
        await db.commit()

        spi = _FakeAgentSession()
        # interrupt=False (default), no process_tree
        await fake_runner.registry.register(
            str(session.id),
            LiveSession(spi_session=spi, capabilities=spi.capabilities),
        )

        resp = await client.post(f"/api/sessions/{session.id}/stop")
        assert resp.status_code == 409
        assert "interrupt" in resp.json()["detail"]
        assert spi.interrupt_calls == 0


class TestPauseResumeProcessControl:
    async def test_pause_calls_process_tree_pause(
        self, client, db, fake_runner,
    ) -> None:
        session = await _seed_session(db, status="running")
        await db.commit()

        spi = _FakeAgentSession()
        proc = _FakeProcessTree()
        await fake_runner.registry.register(
            str(session.id),
            LiveSession(
                spi_session=spi, capabilities=spi.capabilities, process_tree=proc,
            ),
        )

        resp = await client.post(f"/api/sessions/{session.id}/pause")
        assert resp.status_code == 200
        assert resp.json()["status"] == "paused"
        assert proc.pause_calls == 1

    async def test_resume_calls_process_tree_resume(
        self, client, db, fake_runner,
    ) -> None:
        session = await _seed_session(db, status="paused")
        await db.commit()

        spi = _FakeAgentSession()
        proc = _FakeProcessTree()
        await fake_runner.registry.register(
            str(session.id),
            LiveSession(
                spi_session=spi, capabilities=spi.capabilities, process_tree=proc,
            ),
        )

        resp = await client.post(f"/api/sessions/{session.id}/resume")
        assert resp.status_code == 200
        assert resp.json()["status"] == "running"
        assert proc.resume_calls == 1


class TestBrokerLifecycleEvents:
    """§5.1 + §5.2.3: lifecycle changes fan out via the realtime broker
    on the ``session.{id}`` topic, with ``approval_resolved`` and
    ``status_changed`` event payloads."""

    async def test_approve_publishes_approval_resolved(
        self, client, db, fake_runner,
    ) -> None:
        session = await _seed_session(db)
        await _seed_event(db, session, 1, request_id="req-evt")
        await db.commit()

        broker = get_broker()
        _sub_id, frame_iter = await broker.subscribe({f"session.{session.id}"})

        spi = _FakeAgentSession()
        spi.capabilities = BackendCapabilities(approval_hooks=True)
        await fake_runner.registry.register(
            str(session.id),
            LiveSession(spi_session=spi, capabilities=spi.capabilities),
        )

        resp = await client.post(
            f"/api/sessions/{session.id}/approve",
            json={"request_id": "req-evt"},
        )
        assert resp.status_code == 200

        frame = await asyncio.wait_for(frame_iter.__anext__(), timeout=2.0)
        assert frame["topic"] == f"session.{session.id}"
        assert frame["payload"]["event"] == "approval_resolved"
        assert frame["payload"]["request_id"] == "req-evt"
        assert frame["payload"]["decision"] == "approved"

    async def test_stop_publishes_status_changed(
        self, client, db, fake_runner,
    ) -> None:
        session = await _seed_session(db, status="running")
        await db.commit()

        broker = get_broker()
        _sub_id, frame_iter = await broker.subscribe({f"session.{session.id}"})

        resp = await client.post(f"/api/sessions/{session.id}/stop")
        assert resp.status_code == 200

        frame = await asyncio.wait_for(frame_iter.__anext__(), timeout=2.0)
        assert frame["payload"]["event"] == "status_changed"
        assert frame["payload"]["status"] == "stopped"


class TestLiveSessionAbsentIsSilent:
    """When no live session is registered (Phase B cross-process bridge
    case), the endpoints still succeed via the DB-only path — the
    operator's decision is recorded and visible on reconnect."""

    async def test_approve_with_no_live_session_still_succeeds(
        self, client, db, fake_runner,
    ) -> None:
        session = await _seed_session(db)
        await _seed_event(db, session, 1, request_id="req-noop")
        await db.commit()

        # Registry is empty.
        resp = await client.post(
            f"/api/sessions/{session.id}/approve",
            json={"request_id": "req-noop"},
        )
        assert resp.status_code == 200
        assert resp.json()["decision"] == "approved"
