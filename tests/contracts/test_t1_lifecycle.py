"""T1 — Lifecycle: create → send → close; repeated close idempotent.

Verifies:
- create_session() returns a valid AgentSession
- send() populates the event stream
- events() yields at least one event after send
- close() is idempotent (second close does not raise)
"""

from __future__ import annotations

import pytest

from orchestratord.spi.backend import SessionSpec
from orchestratord.spi.events import EventKind


async def test_create_session_returns_valid_session(stub_backend):
    """create_session with a minimal SessionSpec returns a session with an id."""
    spec = SessionSpec(cwd="/tmp")
    session = stub_backend.create_session(spec)
    assert session.session_id
    assert session.session_id.startswith("stub-")


async def test_send_produces_events(stub_backend):
    """After send(), events() yields at least one event."""
    session = stub_backend.create_session(SessionSpec(cwd="/tmp"))
    await session.send("hello")
    events = [e async for e in session.events()]
    assert len(events) >= 1


async def test_send_produces_session_complete(stub_backend):
    """The event stream must end with SESSION_COMPLETE."""
    session = stub_backend.create_session(SessionSpec(cwd="/tmp"))
    await session.send("hello")
    events = [e async for e in session.events()]
    last = events[-1]
    assert last.kind == EventKind.SESSION_COMPLETE


async def test_close_is_idempotent(stub_backend):
    """Calling close() twice must not raise."""
    session = stub_backend.create_session(SessionSpec(cwd="/tmp"))
    await session.close()
    await session.close()  # must not raise


async def test_send_after_close_raises(stub_backend):
    """send() after close() must raise."""
    session = stub_backend.create_session(SessionSpec(cwd="/tmp"))
    await session.close()
    with pytest.raises(Exception):
        await session.send("should fail")