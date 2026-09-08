"""Event-loop responsiveness regression tests (issue #14).

``git status`` subprocesses on a large repository can take seconds.  Every
``get_file_status`` call that runs inline on the asyncio event loop freezes
all sessions, control sockets, and heartbeats for the duration.  All four
call sites (two in ``BackendRunner`` TURN_COMPLETE handling, two in
``Orchestrator._run_issue``) must offload via ``asyncio.to_thread``.

The regression contract pinned here is behavioral: while a slow
``get_file_status`` is in flight, a concurrently-scheduled heartbeat keeps
ticking — the event loop must not be starved.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from orchestratord.backend_runner import BackendRunner
from orchestratord.spi.capabilities import BackendCapabilities
from orchestratord.spi.events import EventEnvelope, EventKind

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _slow_get_file_status(sleep_seconds: float = 2.0):
    """Return a ``get_file_status`` mock that blocks for *sleep_seconds*.

    The mock runs synchronously so that an inline call starves the
    event loop while an ``asyncio.to_thread`` offload keeps it alive.
    """

    def slow(path: str) -> list:
        time.sleep(sleep_seconds)
        return []

    return slow


async def _heartbeat(ticks: list[float], interval: float = 0.05) -> None:
    """Record a monotonic timestamp every *interval* seconds."""
    while True:
        ticks.append(time.monotonic())
        await asyncio.sleep(interval)


def _agent_session() -> SimpleNamespace:
    """Return a minimal session object with attributes ``_process_events``
    touches during a TURN_COMPLETE → SESSION_COMPLETE sequence."""
    return SimpleNamespace(
        run_id="run-git-status",
        issue=SimpleNamespace(id="1"),
        workspace=SimpleNamespace(path=Path("/tmp")),
        status="running",
        session_end_reason=None,
        session_end_summary=None,
        output_text="",
        turn_count=2,
        tool_count=0,
        control_socket=SimpleNamespace(_command_queue=asyncio.Queue()),
        paused=False,
        pause_reason="",
        prompt_override="",
        _pause_gate=None,
        state_cache=None,
        _on_pause_state_change=None,
        _pending_followups=[],
        _transcript_storage=None,
        conversation_id=None,
        backend_name=None,
        backend_session_id=None,
        stage_id=None,
        branch_id=None,
        parent_run_id=None,
        session_id=None,
        cost_usd=0.0,
        token_usage={},
    )


def _timeouts() -> dict[str, float]:
    return {
        "total": 30.0,
        "handshake": 30.0,
        "first_turn": 30.0,
        "inactivity": 30.0,
        "idle_watchdog": 30.0,
    }


# ---------------------------------------------------------------------------
# Test 1: _check_file_changes (every TURN_COMPLETE path)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_check_file_changes_offloads_slow_git_status_to_thread(monkeypatch):
    """``_check_file_changes`` (invoked on every TURN_COMPLETE) must offload
    ``get_file_status`` to a worker thread.

    Without the offload the mocked 2 s ``git status`` blocks the event loop
    and the heartbeat below never ticks; with it the loop stays free.
    """
    monkeypatch.setattr(
        "orchestratord.git.utils.get_file_status",
        _slow_get_file_status(2.0),
    )

    runner = object.__new__(BackendRunner)
    session = SimpleNamespace(workspace=SimpleNamespace(path="/tmp"))

    ticks: list[float] = []
    hb = asyncio.create_task(_heartbeat(ticks))
    try:
        changed = await asyncio.wait_for(
            runner._check_file_changes(session, None),
            timeout=5.0,
        )
        assert changed is False
    finally:
        hb.cancel()
        await asyncio.gather(hb, return_exceptions=True)

    assert len(ticks) >= 10, (
        "event loop was starved by an inline get_file_status: expected the "
        f"heartbeat to keep ticking during the 2 s status call, got "
        f"{len(ticks)} ticks"
    )


# ---------------------------------------------------------------------------
# Test 2: Full TURN_COMPLETE dispatch (file-change check + read-only guard)
# ---------------------------------------------------------------------------


class _TurnCompleteSpiSession:
    """spi_session yielding TOOL_CALL → TURN_COMPLETE → SESSION_COMPLETE."""

    async def events(self):
        yield EventEnvelope(
            seq=1,
            timestamp=0.0,
            kind=EventKind.TOOL_CALL,
            payload={"name": "bash", "call_id": "c1", "arguments": {}},
        )
        yield EventEnvelope(
            seq=2,
            timestamp=0.0,
            kind=EventKind.TURN_COMPLETE,
            payload={"turn": 2},
        )
        yield EventEnvelope(
            seq=3,
            timestamp=0.0,
            kind=EventKind.SESSION_COMPLETE,
            payload={"reason": "success"},
        )


def _runner_with_fake_policy() -> BackendRunner:
    """Return a ``BackendRunner`` instance with a fake approval policy
    so TOOL_CALL events can be dispatched without crashing."""
    runner = object.__new__(BackendRunner)
    runner.backend = SimpleNamespace(
        capabilities=lambda: BackendCapabilities(approval_hooks=False),
    )
    runner._approval_policy = SimpleNamespace(
        evaluate=lambda event, ctx: event.allow(),
    )
    return runner


@pytest.mark.asyncio
async def test_turn_complete_dispatch_does_not_block_event_loop(monkeypatch):
    """Full TURN_COMPLETE dispatch (file-change check + read-only spiral
    guard) must not freeze the event loop while a slow ``git status`` runs.

    Both the ``_check_file_changes`` call and the read-only spiral guard
    call ``get_file_status``.  A slow mock must not prevent a concurrently-
    scheduled heartbeat from ticking.
    """
    monkeypatch.setattr(
        "orchestratord.git.utils.get_file_status",
        _slow_get_file_status(2.0),
    )

    runner = _runner_with_fake_policy()
    session = _agent_session()

    ticks: list[float] = []
    hb = asyncio.create_task(_heartbeat(ticks))
    try:
        await asyncio.wait_for(
            runner._process_events(
                _TurnCompleteSpiSession(),
                session,
                {},
                None,
                None,
                None,
                None,
                timeouts=_timeouts(),
            ),
            timeout=8.0,
        )
    finally:
        hb.cancel()
        await asyncio.gather(hb, return_exceptions=True)

    assert len(ticks) >= 10, (
        "event loop was starved by inline get_file_status calls during "
        f"TURN_COMPLETE dispatch: expected heartbeat to keep ticking, got "
        f"{len(ticks)} ticks"
    )