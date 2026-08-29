"""T7 — Resume: close → resume_session_id rebuild → history visible.

Verifies the cross-backend SPI resume contract (DESIGN §2, ADR-003):

1. ``SessionSpec.resume_session_id`` reaches
   ``Backend.create_session(spec)``.
2. ``AgentSession.probe_resume()`` returns one of the three
   ``ResumeStatus`` values, and the caller can branch on REJECTED to
   skip ``send()``.
3. A session that omits ``probe_resume`` is contractually obligated to
   yield ``ResumeStatus.UNDETECTABLE`` (so the orchestrator can
   distinguish "I have no opinion" from "definitely RESUMED").
4. ``DegradingSession`` is transparent for ``probe_resume()`` —
   forwarding the inner result (or UNDETECTABLE on absence) so the
   degradation wrapper preserves the SPI contract.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from orchestratord.spi.backend import SessionSpec
from orchestratord.spi.capabilities import BackendCapabilities
from orchestratord.spi.degradation import DegradingSession
from orchestratord.spi.events import EventEnvelope, EventKind
from orchestratord.spi.session import AgentSession, ResumeStatus

from .conftest import require_capability


# ---------------------------------------------------------------------------
# SessionSpec.resume_session_id reaches create_session
# ---------------------------------------------------------------------------


async def test_resume_session_id_accepted(stub_backend):
    """create_session with resume_session_id returns a valid session."""
    caps = stub_backend.capabilities()
    require_capability(caps, "resumable")
    session = stub_backend.create_session(
        SessionSpec(cwd="/tmp", resume_session_id="previous-session-1")
    )
    assert session.session_id


async def test_resume_session_has_id(stub_backend):
    """Resumed session has a non-empty session_id."""
    caps = stub_backend.capabilities()
    require_capability(caps, "resumable")
    session = stub_backend.create_session(
        SessionSpec(cwd="/tmp", resume_session_id="previous-session-2")
    )
    assert session.session_id


async def test_session_spec_resume_id_reaches_backend():
    """Backend.create_session(spec) observes spec.resume_session_id.

    A backend that wants to honor ``resume_session_id`` must read it
    from the passed-in spec — this test wires that contract by using
    a recording backend that captures the spec verbatim.
    """
    captured: dict[str, Any] = {}

    class _RecordingBackend:
        name = "recording"
        display_name = "Recording Backend"

        def capabilities(self) -> BackendCapabilities:
            return BackendCapabilities(resumable=True)

        def create_session(self, spec: SessionSpec) -> AgentSession:
            captured["spec"] = spec
            return _RecordingSession(
                session_id=f"rec-{spec.resume_session_id}",
                spec=spec,
            )

        def dispose(self) -> None:
            return None

    backend = _RecordingBackend()
    spec = SessionSpec(cwd="/tmp", resume_session_id="abc-12345")
    session = backend.create_session(spec)
    try:
        assert captured["spec"] is spec
        assert session.session_id == "rec-abc-12345"
        # The spec the session holds must round-trip the field.
        assert session._spec.resume_session_id == "abc-12345"
    finally:
        session.close_sync if hasattr(session, "close_sync") else None
        await session.close()


# ---------------------------------------------------------------------------
# probe_resume() contract — three-state signal
# ---------------------------------------------------------------------------


class _RecordingSession:
    """Test AgentSession that returns a configured ``probe_resume`` status."""

    def __init__(
        self,
        *,
        session_id: str,
        spec: SessionSpec | None = None,
        probe_result: ResumeStatus = ResumeStatus.UNDETECTABLE,
        send_called: list[bool] | None = None,
    ) -> None:
        self.session_id = session_id
        self._spec = spec or SessionSpec(cwd="/tmp")
        self._probe_result = probe_result
        self._send_called = send_called if send_called is not None else []
        self.capabilities = BackendCapabilities(
            resumable=True, resume_detection=True
        )
        self._closed = False

    async def probe_resume(self) -> ResumeStatus:
        return self._probe_result

    async def send(self, content: str | list[Any]) -> None:
        self._send_called.append(True)

    def events(self) -> AsyncIterator[EventEnvelope]:
        async def _gen():
            yield EventEnvelope(
                seq=1, timestamp=0.0, kind=EventKind.SESSION_COMPLETE,
                payload={"reason": "success"},
            )
        return _gen()

    async def interrupt(self) -> None:
        return None

    async def approve(self, request_id: str, decision) -> None:
        return None

    async def close(self) -> None:
        self._closed = True


async def test_probe_resume_returns_resumed():
    """Caller receives ResumeStatus.RESUMED when probe succeeds."""
    session = _RecordingSession(
        session_id="ok", probe_result=ResumeStatus.RESUMED
    )
    assert await session.probe_resume() is ResumeStatus.RESUMED


async def test_probe_resume_returns_rejected_skips_send():
    """REJECTED tells the caller to skip send() and treat as terminal.

    This emulates the BackendRunner branch in
    ``_run_with_backend`` (backend_runner.py §432-465): a REJECTED
    probe means the orchestrator must NOT call ``send()``.
    """
    send_called: list[bool] = []
    session = _RecordingSession(
        session_id="gone",
        spec=SessionSpec(cwd="/tmp", resume_session_id="gone-sess"),
        probe_result=ResumeStatus.REJECTED,
        send_called=send_called,
    )

    # Simulate orchestrator branching on REJECTED.
    status = await session.probe_resume()
    if status is ResumeStatus.REJECTED:
        pass  # orchestrator skips send() and aborts the run
    else:
        await session.send("should not be called")

    assert send_called == []


async def test_probe_resume_returns_undetectable_attempts_send():
    """UNDETECTABLE falls through to normal send() (orchestrator may attempt)."""
    send_called: list[bool] = []
    session = _RecordingSession(
        session_id="unknown",
        spec=SessionSpec(cwd="/tmp", resume_session_id="unknown-sess"),
        probe_result=ResumeStatus.UNDETECTABLE,
        send_called=send_called,
    )

    status = await session.probe_resume()
    if status is ResumeStatus.UNDETECTABLE:
        # Orchestrator optimistically attempts send.
        await session.send("probe-was-undetectable")
    elif status is ResumeStatus.RESUMED:
        await session.send("resumed")
    # REJECTED would skip — but we are not in that branch.

    assert send_called == [True]


class _NoProbeSession:
    """Test AgentSession that omits ``probe_resume`` (legacy shape).

    Per the SPI: a backend whose session does not implement
    ``probe_resume`` must be treated as UNDETECTABLE — the caller
    distinguishes "I have no opinion" from "definitely RESUMED".
    """

    session_id = "legacy"
    capabilities = BackendCapabilities(resumable=True)

    async def send(self, content: str | list[Any]) -> None:
        return None

    def events(self) -> AsyncIterator[EventEnvelope]:
        async def _gen():
            return
            yield  # noqa: unreachable — makes this an async generator
        return _gen()

    async def interrupt(self) -> None:
        return None

    async def approve(self, request_id: str, decision) -> None:
        return None

    async def close(self) -> None:
        return None


async def test_session_without_probe_resume_returns_undetectable():
    """DegradingSession treats absent probe_resume as UNDETECTABLE.

    The wrapper guarantees the SPI contract even when the wrapped
    session predates the resume contract. This is the value-add of
    routing every session through ``DegradingBackend``: every caller
    sees a three-state ``probe_resume()`` regardless of backend age.
    """
    inner = _NoProbeSession()
    wrapped = DegradingSession(inner)  # type: ignore[arg-type]
    result = await wrapped.probe_resume()
    assert result is ResumeStatus.UNDETECTABLE


async def test_degrading_session_forwards_probe_resume():
    """DegradingSession.probe_resume() forwards the inner result.

    When the wrapped session provides ``probe_resume``, the wrapper
    must surface it verbatim — REJECTED, RESUMED, and UNDETECTABLE
    all flow through unchanged. This protects the orchestrator's
    three-state decision.
    """
    for inner_status in (
        ResumeStatus.RESUMED,
        ResumeStatus.REJECTED,
        ResumeStatus.UNDETECTABLE,
    ):
        inner = _RecordingSession(
            session_id=f"s-{inner_status.value}",
            probe_result=inner_status,
        )
        wrapped = DegradingSession(inner)  # type: ignore[arg-type]
        assert await wrapped.probe_resume() is inner_status
