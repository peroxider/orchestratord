"""Tests for Scheme C: dsh ERROR propagation.

Verifies that:

1. ``DshSession.send()`` surfaces SDK exceptions as ``EventKind.ERROR``
   events with ``code="dsh_error"`` and a ``SESSION_COMPLETE`` with
   ``reason="error"`` — instead of silently swallowing the exception
   into a normal-looking terminal event (the historical behaviour).

2. ``DshSession.send()`` surfaces harness-init failures (model name
   unknown, SDK version mismatch) as ``EventKind.ERROR`` with
   ``code="dsh_init_error"``.

3. ``DshSession._ingest_result`` translates an abnormal
   ``finish_reason`` into an ``EventKind.ERROR`` event without
   double-emitting ``SESSION_COMPLETE`` (the historical double-emit
   bug — see Scheme C §3.6).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

import pytest

from orchestratord.spi.backend import SessionSpec
from orchestratord.spi.events import EventKind

from orchestratord_dsh.session import DshSession


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

    def run(self, text: str, session_id: str | None) -> _FakeResult:
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
        # Skip the cost probe (which expects a real ``sample_run``) by
        # marking it already done.
        session._cost_probed = True
        return session

    return _factory


def _drain(session: DshSession) -> list[Any]:
    """Run the async generator returned by ``events()`` to completion."""

    out: list[Any] = []

    async def collect() -> None:
        async for ev in session.events():
            out.append(ev)

    asyncio.run(collect())
    return out


def test_send_translates_sdk_exception_to_error(stub_harness) -> None:
    """An SDK exception in ``harness.run`` must produce ERROR then
    SESSION_COMPLETE(reason='error'); no other terminal events.
    """
    session = stub_harness(result=RuntimeError("rate limit"))

    asyncio.run(session.send("hello"))

    events = _drain(session)
    kinds = [e.kind for e in events]
    assert kinds[0] == EventKind.ERROR, kinds
    assert kinds[-1] == EventKind.SESSION_COMPLETE, kinds
    assert events[-1].payload == {"reason": "error"}

    err = events[0]
    assert err.payload["code"] == "dsh_error"
    assert "RuntimeError" in err.payload["message"]
    assert "rate limit" in err.payload["message"]

    # ``_send_sync`` must have been called once with the text and
    # session id, and the harness must not have been closed inside
    # ``send`` (close() is the caller's responsibility).
    assert session._harness.run_calls == [("hello", session.session_id)]
    assert session._harness.closed is False

    # No double-emit: exactly one SESSION_COMPLETE, one ERROR.
    assert sum(1 for e in events if e.kind == EventKind.SESSION_COMPLETE) == 1
    assert sum(1 for e in events if e.kind == EventKind.ERROR) == 1


def test_send_translates_init_failure_to_error() -> None:
    """If ``_get_harness`` itself fails (SDK import error, bad model
    name), the failure surfaces as ``dsh_init_error`` and no
    ``SESSION_COMPLETE`` is missed.
    """
    from orchestratord_dsh import session as session_mod

    session = DshSession(_spec())
    original = session_mod.DshSession._get_harness

    def _explode(self: Any) -> Any:  # noqa: ARG001
        raise ValueError("unknown model: bogus")

    try:
        session_mod.DshSession._get_harness = _explode  # type: ignore[assignment]
        asyncio.run(session.send("hi"))
    finally:
        session_mod.DshSession._get_harness = original  # type: ignore[assignment]

    events = _drain(session)
    kinds = [e.kind for e in events]
    assert kinds == [EventKind.ERROR, EventKind.SESSION_COMPLETE]
    assert events[0].payload["code"] == "dsh_init_error"
    assert events[-1].payload == {"reason": "error"}


def test_ingest_result_emits_error_on_abnormal_finish(stub_harness) -> None:
    """When ``finish_reason`` is abnormal but ``result.events`` is
    empty, ``_ingest_result`` appends a synthetic ``dsh_finish`` ERROR
    event. ``SESSION_COMPLETE`` is the caller's responsibility (i.e.
    ``send``'s finally); ``_ingest_result`` must NOT emit one.
    """
    session = stub_harness(result=_FakeResult(events=[], finish_reason="error"))

    asyncio.run(session.send("hi"))

    events = _drain(session)
    finish_error = [e for e in events if e.kind == EventKind.ERROR]
    assert len(finish_error) == 1
    assert finish_error[0].payload == {
        "code": "dsh_finish",
        "reason": "error",
    }
    # The SESSION_COMPLETE comes from send()'s finally, reason="error"
    # because the ERROR we just appended is the latest event when
    # ``send`` decides.
    terminal = [e for e in events if e.kind == EventKind.SESSION_COMPLETE]
    assert len(terminal) == 1
    assert terminal[0].payload["reason"] == "error"


def test_ingest_result_success_emits_no_error(stub_harness) -> None:
    """The success path must keep emitting only the events the SDK
    already produced — no synthetic ERROR, no synthetic SESSION_COMPLETE
    inside ``_ingest_result``.
    """
    session = stub_harness(
        result=_FakeResult(
            events=[
                {"type": "assistant/message", "data": {"message": {"content": []}}},
            ],
            finish_reason="success",
        )
    )

    asyncio.run(session.send("hi"))

    events = _drain(session)
    assert [e.kind for e in events] == [EventKind.TEXT, EventKind.SESSION_COMPLETE]
    assert events[-1].payload == {"reason": "success"}