"""Tests for Scheme C: dsh ERROR propagation.

Verifies that:

1. ``DshSession.send()`` surfaces SDK exceptions as ``EventKind.ERROR``
   events with ``code="dsh_error"`` and a ``SESSION_COMPLETE`` with
   ``reason="error"`` — instead of silently swallowing the exception
   into a normal-looking terminal event (the historical behaviour).

2. ``DshSession.send()`` surfaces harness-init failures (model name
   unknown, SDK version mismatch) as ``EventKind.ERROR`` with
   ``code="dsh_init_error"``.

3. An abnormal ``finish_reason`` translates into an ``EventKind.ERROR``
   event without double-emitting ``SESSION_COMPLETE`` (the historical
   double-emit bug — see Scheme C §3.6).

Since the streaming pump, ``send()`` dispatches the turn to a
worker thread and events flow through ``events()``; the tests drain on
the same event loop that started the turn.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

import pytest
from orchestratord_dsh.session import DshSession

from orchestratord.spi.backend import SessionSpec
from orchestratord.spi.events import EventKind


@dataclass
class _FakeResult:
    events: list[dict[str, Any]] = field(default_factory=list)
    finish_reason: str | None = "success"


class _StubHarness:
    """Stand-in for the deepseek-harness SDK harness object."""

    def __init__(self, *, run_result: _FakeResult | Exception) -> None:
        self._run_result = run_result
        self.closed = False
        self.run_calls: list[tuple[str, str | None]] = []

    def run(
        self,
        text: str,
        *,
        session_id: str | None = None,
        on_notification=None,
    ) -> _FakeResult:
        self.run_calls.append((text, session_id))
        if isinstance(self._run_result, Exception):
            raise self._run_result
        return self._run_result

    def start(self) -> None:  # pragma: no cover - trivial
        return None

    def close(self) -> None:
        self.closed = True


def _spec() -> SessionSpec:
    return SessionSpec(cwd="/tmp")


@pytest.fixture
def stub_harness():
    """Factory: ``stub_harness(result=...)`` returns a session preloaded
    with a stub harness that yields the given result (or raises the
    given exception when ``run`` is called).
    """

    def _factory(*, result: _FakeResult | Exception) -> DshSession:
        session = DshSession(_spec())
        session._harness = _StubHarness(run_result=result)
        return session

    return _factory


def _send_and_drain(session: DshSession, text: str = "hello") -> list[Any]:
    """Send on the running loop, then drain the stream to completion.

    The pump delivers events on the loop that started the turn, so the
    send and the drain must share one ``asyncio.run``.
    """
    out: list[Any] = []

    async def main() -> None:
        await session.send(text)
        async for ev in session.events():
            out.append(ev)

    asyncio.run(main())
    return out


def test_send_translates_sdk_exception_to_error(stub_harness) -> None:
    """An SDK exception in ``harness.run`` must produce ERROR then
    SESSION_COMPLETE(reason='error'); no other terminal events.
    """
    session = stub_harness(result=RuntimeError("rate limit"))

    events = _send_and_drain(session)

    kinds = [e.kind for e in events]
    assert kinds[0] == EventKind.ERROR, kinds
    assert kinds[-1] == EventKind.SESSION_COMPLETE, kinds
    assert events[-1].payload == {"reason": "error"}

    err = events[0]
    assert err.payload["code"] == "dsh_error"
    assert "RuntimeError" in err.payload["message"]
    assert "rate limit" in err.payload["message"]

    # The turn must have been dispatched once with the text and session
    # id, and the harness must not have been closed inside ``send``
    # (close() is the caller's responsibility).
    assert session._harness.run_calls == [("hello", session.session_id)]
    assert session._harness.closed is False

    # No double-emit: exactly one SESSION_COMPLETE, one ERROR.
    assert sum(1 for e in events if e.kind == EventKind.SESSION_COMPLETE) == 1
    assert sum(1 for e in events if e.kind == EventKind.ERROR) == 1


def test_send_translates_init_failure_to_error() -> None:
    """If harness construction itself fails (SDK import error, bad model
    name), the failure surfaces as ``dsh_init_error`` and no
    ``SESSION_COMPLETE`` is missed.
    """
    def _explode() -> Any:
        raise ValueError("unknown model: bogus")

    session = DshSession(_spec(), harness_factory=_explode)

    events = _send_and_drain(session, "hi")

    kinds = [e.kind for e in events]
    assert kinds == [EventKind.ERROR, EventKind.SESSION_COMPLETE]
    assert events[0].payload["code"] == "dsh_init_error"
    assert events[-1].payload == {"reason": "error"}


def test_ingest_result_emits_error_on_abnormal_finish(stub_harness) -> None:
    """When ``finish_reason`` is abnormal but ``result.events`` is
    empty, the turn emits a synthetic ``dsh_finish`` ERROR event.
    ``SESSION_COMPLETE`` is emitted exactly once with reason="error".
    """
    session = stub_harness(result=_FakeResult(events=[], finish_reason="error"))

    events = _send_and_drain(session, "hi")

    finish_error = [e for e in events if e.kind == EventKind.ERROR]
    assert len(finish_error) == 1
    assert finish_error[0].payload["code"] == "dsh_finish"
    assert finish_error[0].payload["reason"] == "error"
    # Dsh_finish always carries a message for the core.
    assert isinstance(finish_error[0].payload.get("message"), str)
    assert finish_error[0].payload["message"]
    terminal = [e for e in events if e.kind == EventKind.SESSION_COMPLETE]
    assert len(terminal) == 1
    assert terminal[0].payload["reason"] == "error"


def test_ingest_result_success_emits_no_error(stub_harness) -> None:
    """The success path must keep emitting only the events the SDK
    already produced — no synthetic ERROR, and exactly one
    SESSION_COMPLETE with reason="success".

    The batched ``result.events`` are not re-emitted by the pump path,
    so the stub result's event list is intentionally empty here; the
    incremental callback stream is what feeds ``events()``.
    """
    session = stub_harness(result=_FakeResult(events=[], finish_reason="success"))

    events = _send_and_drain(session, "hi")

    assert [e.kind for e in events] == [EventKind.SESSION_COMPLETE]
    assert events[-1].payload == {"reason": "success"}
