"""D5 regression tests: dsh close() reaps the turn task and unregisters.

Review finding (2026-09-08, D5): two resource-lifecycle defects in the
dsh backend —

1. ``DshSession.close()`` did not reap an in-flight turn task: the
   ``asyncio.to_thread`` worker (non-daemon thread) stayed parked on an
   unlocked tool read, leaving the task pending and the runtime/tool
   processes alive until process exit.
2. ``DshBackend._sessions`` only grew: a closed session's spec
   (containing env / api_key) stayed resident for the backend's life.

Each test below fails on the pre-fix code and pins the fixed contract.
"""

from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace

import pytest
from orchestratord_dsh.backend import DshBackend
from orchestratord_dsh.session import DshSession

from orchestratord.spi.backend import SessionSpec


class BlockingHarness:
    """Fake harness whose turn parks the worker like an in-flight tool read.

    ``run()`` blocks on an event instead of returning, so the turn task
    stays pending until the test releases it. There is no subprocess, so
    ``close()`` cannot unblock the read by killing a process tree — the
    reaping must come from ``close()`` itself cancelling the turn task.
    """

    def __init__(self) -> None:
        self.unblock = threading.Event()
        self.started = False
        self.closed = False
        self.client = SimpleNamespace(
            start=lambda: None,
            close=lambda: None,
            _proc=None,
        )

    def start(self) -> None:
        pass

    def run(self, input, *, session_id=None, on_notification=None):
        self.started = True
        self.unblock.wait(timeout=30)
        return SimpleNamespace(finish_reason="completed")

    def close(self) -> None:
        self.closed = True


class CompletingHarness:
    """Fake harness whose turn returns immediately (normal completion path)."""

    def __init__(self) -> None:
        self.client = SimpleNamespace(
            start=lambda: None,
            close=lambda: None,
            _proc=None,
        )
        self.closed = False

    def start(self) -> None:
        pass

    def run(self, input, *, session_id=None, on_notification=None):
        return SimpleNamespace(finish_reason="completed")

    def close(self) -> None:
        self.closed = True


def _spec() -> SessionSpec:
    return SessionSpec(
        cwd="/tmp", model="deepseek-v4-flash", provider="deepseek-official"
    )


async def _until(predicate, timeout: float = 3.0) -> None:
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_close_reaps_pending_turn_task() -> None:
    """close() must cancel + join an in-flight turn task (no pending task).

    Pre-fix close() only *shielded* the turn task — a worker parked on an
    unlocked tool read left the task pending and made close() raise
    TimeoutError after 5s instead of reaping it.
    """
    harness = BlockingHarness()
    session = DshSession(_spec(), harness_factory=lambda: harness)
    try:
        await session.send("task")
        await _until(lambda: harness.started, timeout=3.0)
        await session.close()
        assert session._turn_task is not None
        assert session._turn_task.done(), "close() must reap the in-flight turn task"
    finally:
        harness.unblock.set()  # release the parked worker thread so it can exit


@pytest.mark.asyncio
async def test_backend_unregisters_closed_session_after_send(monkeypatch) -> None:
    """create → send → close: turn task reaped AND session removed from _sessions."""
    harness = CompletingHarness()
    monkeypatch.setattr("deepseek_harness.api.DeepSeekHarness", lambda config: harness)
    backend = DshBackend()
    session = backend.create_session(_spec())
    assert session in backend._sessions
    await session.send("task")
    assert session._turn_task is not None
    await session.close()
    assert session._turn_task.done(), "turn task must be reaped by close()"
    assert session not in backend._sessions, (
        "closed session spec (env / api_key) must not stay resident in _sessions"
    )


@pytest.mark.asyncio
async def test_close_is_idempotent_and_unregisters() -> None:
    """A second close() must not raise (D5 acceptance #2)."""
    backend = DshBackend()
    session = backend.create_session(_spec())
    await session.close()
    await session.close()  # must not raise
    assert session not in backend._sessions
