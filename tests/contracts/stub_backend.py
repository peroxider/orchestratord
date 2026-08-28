"""In-memory stub backend for verifying the contract test framework.

This backend responds to ``send()`` with a fixed sequence of events
and supports all capability bits so that every contract test group can
exercise its assertions.  It is NOT a real agent — it exists only to
validate that the contract test harness itself is correct.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from typing import Any

from orchestratord.spi.approval import ApprovalDecision, ApprovalRequest
from orchestratord.spi.backend import AgentBackend, SessionSpec
from orchestratord.spi.capabilities import BackendCapabilities
from orchestratord.spi.events import EventEnvelope, EventKind
from orchestratord.spi.session import AgentSession


class StubSession:
    """A fake AgentSession that emits a canned event sequence."""

    def __init__(self, session_id: str, canned_events: list[EventEnvelope] | None = None) -> None:
        self.session_id = session_id
        self.capabilities = BackendCapabilities(
            streaming_deltas=True,
            resumable=True,
            interrupt=True,
            approval_hooks=True,
            parallel_sessions=True,
            cost_reporting=True,
            tool_filtering=True,
            takeover=True,
        )
        self._canned = list(canned_events or [])
        self._pending_approval: ApprovalRequest | None = None
        self._interrupted = False
        self._closed = False
        self._seq = 0

    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq

    def _now(self) -> float:
        return time.time()

    async def send(self, content: str | list[Any]) -> None:
        if self._closed:
            raise RuntimeError("session closed")
        if not self._canned:
            self._canned = [
                EventEnvelope(
                    seq=self._next_seq(), timestamp=self._now(),
                    kind=EventKind.TEXT, payload={"text": f"echo: {content}"},
                ),
                EventEnvelope(
                    seq=self._next_seq(), timestamp=self._now(),
                    kind=EventKind.TURN_COMPLETE, payload={"reason": "success"},
                ),
                EventEnvelope(
                    seq=self._next_seq(), timestamp=self._now(),
                    kind=EventKind.SESSION_COMPLETE, payload={"reason": "success"},
                ),
            ]

    async def _emit_canned(self):
        for ev in self._canned:
            yield ev
        self._canned.clear()

    def events(self) -> AsyncIterator[EventEnvelope]:
        return self._emit_canned()

    async def interrupt(self) -> None:
        self._interrupted = True

    async def approve(self, request_id: str, decision: ApprovalDecision) -> None:
        self._pending_approval = None

    async def close(self) -> None:
        self._closed = True


class StubBackend:
    """A fake AgentBackend that creates StubSessions."""

    name = "stub"
    display_name = "Stub Backend (contract test)"

    def __init__(self) -> None:
        self._sessions: list[StubSession] = []
        self._disposed = False

    def capabilities(self) -> BackendCapabilities:
        return BackendCapabilities(
            streaming_deltas=True,
            resumable=True,
            interrupt=True,
            approval_hooks=True,
            parallel_sessions=True,
            cost_reporting=True,
            tool_filtering=True,
            takeover=True,
        )

    def create_session(self, spec: SessionSpec) -> AgentSession:
        sid = spec.resume_session_id or f"stub-{len(self._sessions)}"
        session = StubSession(sid)
        self._sessions.append(session)
        return session

    def dispose(self) -> None:
        self._disposed = True
        self._sessions.clear()