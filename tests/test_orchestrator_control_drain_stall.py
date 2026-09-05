"""Control plane must stay alive during event stalls.

Historical defect: control commands are only drained inside the
``_process_events`` event loop, which only iterates when an event
arrives. With a batch/stalling backend the loop blocks in ``anext`` —
``stop``/``pause``/``inject`` commands sit unconsumed for the whole
stall (measured: 3m53s on a real dsh issue), and the idle-watchdog
timeout can never fire because ``gap`` is recomputed against a
``last_event_monotonic`` that was just reset to ``now``.

Contract pinned here (seam: ``BackendRunner._process_events`` with a
stalling spi_session and a control socket queue):

1. A queued ``stop`` command must be honored while NO events are
   arriving (within one poll interval, well before the stall ends).
2. The idle-watchdog timeout must fire during total silence instead of
   hanging until the next event.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from orchestratord.backend_runner import BackendRunner
from orchestratord.control_socket import ControlCommand
from orchestratord.spi.events import EventEnvelope, EventKind


class _StalledSpiSession:
    """spi_session whose events() stays silent for ``stall_seconds``."""

    def __init__(self, stall_seconds: float) -> None:
        self._stall = stall_seconds

    async def events(self):
        await asyncio.sleep(self._stall)
        yield EventEnvelope(
            seq=1,
            timestamp=0,
            kind=EventKind.SESSION_COMPLETE,
            payload={"reason": "success"},
        )


class _ImmediateSpiSession:
    capabilities = SimpleNamespace(pausable=True)

    async def pause(self):
        pass

    async def resume(self):
        pass

    async def events(self):
        yield EventEnvelope(
            seq=1,
            timestamp=0,
            kind=EventKind.TEXT_DELTA,
            payload={"delta": "must wait for resume"},
        )
        yield EventEnvelope(
            seq=2,
            timestamp=0,
            kind=EventKind.SESSION_COMPLETE,
            payload={"reason": "success"},
        )


@pytest.mark.asyncio
async def test_native_pause_is_applied_before_registry_notification():
    session = _agent_session(with_stop=False)
    applied = []

    class Native(_ImmediateSpiSession):
        async def pause(self):
            assert session.paused is False
            applied.append("native_pause")

    session._on_pause_state_change = lambda *_: applied.append("registry_pause")
    session.control_socket._command_queue.put_nowait(ControlCommand(cmd="pause"))
    await _runner()._drain_backend_controls(Native(), session)
    assert applied == ["native_pause", "registry_pause"]
    assert session.paused


@pytest.mark.asyncio
async def test_rejected_native_pause_does_not_publish_paused():
    session = _agent_session(with_stop=False)

    class Native(_ImmediateSpiSession):
        async def pause(self):
            raise RuntimeError("test permission denied")

    session.control_socket._command_queue.put_nowait(ControlCommand(cmd="pause"))
    await _runner()._drain_backend_controls(Native(), session)
    assert not session.paused


@pytest.mark.asyncio
async def test_backend_without_native_pause_cannot_fake_success():
    session = _agent_session(with_stop=False)
    session.control_socket._command_queue.put_nowait(ControlCommand(cmd="pause"))
    await _runner()._drain_backend_controls(_StalledSpiSession(1), session)
    assert not session.paused


def _agent_session(with_stop: bool) -> SimpleNamespace:
    queue: asyncio.Queue[ControlCommand] = asyncio.Queue()
    if with_stop:
        queue.put_nowait(ControlCommand(cmd="stop"))
    return SimpleNamespace(
        run_id="run-stall",
        issue=SimpleNamespace(id="1"),
        workspace=SimpleNamespace(path=Path("/tmp")),
        status="running",
        session_end_reason=None,
        session_end_summary=None,
        output_text="",
        turn_count=0,
        tool_count=0,
        control_socket=SimpleNamespace(_command_queue=queue),
        pause_resume_event=None,
        paused=False,
        pause_reason="",
        prompt_override="",
        _pause_gate=None,
        state_cache=None,
        _on_pause_state_change=None,
        _pending_followups=[],
        _transcript_storage=None,
    )


@pytest.mark.asyncio
async def test_pause_blocks_backend_events_until_resume() -> None:
    """Pause must stop event consumption, not merely change registry metadata."""
    session = _agent_session(with_stop=False)
    session.control_socket._command_queue.put_nowait(ControlCommand(cmd="pause"))
    task = asyncio.create_task(
        _runner()._process_events(
            _ImmediateSpiSession(),
            session,
            {},
            None,
            None,
            None,
            None,
            timeouts=_timeouts(),
        )
    )

    await asyncio.sleep(0.35)
    assert task.done() is False
    assert session.output_text == ""
    assert session.paused is True

    session.control_socket._command_queue.put_nowait(ControlCommand(cmd="resume"))
    await asyncio.wait_for(task, timeout=2.0)

    assert session.paused is False
    assert session.output_text == "must wait for resume"


def _timeouts(**overrides: float) -> dict[str, float]:
    base = {
        "total": 30.0,
        "handshake": 30.0,
        "first_turn": 30.0,
        "inactivity": 30.0,
        "idle_watchdog": 30.0,
    }
    base.update(overrides)
    return base


def _runner() -> BackendRunner:
    runner = object.__new__(BackendRunner)
    runner._check_file_changes = lambda *_args: asyncio.sleep(0, result=True)  # type: ignore[method-assign]
    return runner


@pytest.mark.asyncio
async def test_stop_is_honored_during_event_stall() -> None:
    """stop must be consumed within ~1 poll interval while the backend
    is silent — not deferred until the next event arrives.
    """
    # The backend stays silent for 8s; the stop command is queued now.
    spi_session = _StalledSpiSession(stall_seconds=8.0)
    session = _agent_session(with_stop=True)

    await asyncio.wait_for(
        _runner()._process_events(
            spi_session,
            session,
            {},
            None,
            None,
            None,
            None,
            timeouts=_timeouts(),
        ),
        timeout=2.0,
    )

    assert session.status == "failed"
    assert session.session_end_reason == "operator_stop"


@pytest.mark.asyncio
async def test_idle_watchdog_fires_during_total_silence() -> None:
    """With no events at all, idle_watchdog must terminate the loop."""
    spi_session = _StalledSpiSession(stall_seconds=999.0)
    session = _agent_session(with_stop=False)

    await asyncio.wait_for(
        _runner()._process_events(
            spi_session,
            session,
            {},
            None,
            None,
            None,
            None,
            timeouts=_timeouts(idle_watchdog=0.5),
        ),
        timeout=3.0,
    )

    assert session.status == "failed"
    assert session.session_end_reason == "idle_watchdog_timeout"


@pytest.mark.asyncio
async def test_stop_still_honored_between_events() -> None:
    """Existing behavior must not regress: stop arriving between events
    breaks the loop at the next drain point.
    """
    spi_session = _StalledSpiSession(stall_seconds=0.0)
    session = _agent_session(with_stop=True)

    await asyncio.wait_for(
        _runner()._process_events(
            spi_session,
            session,
            {},
            None,
            None,
            None,
            None,
            timeouts=_timeouts(),
        ),
        timeout=2.0,
    )

    assert session.status == "failed"
    assert session.session_end_reason == "operator_stop"


@pytest.mark.asyncio
async def test_first_turn_timeout_does_not_fire_while_streaming() -> None:
    """A backend that streams actively but has not reached its first
    TURN_COMPLETE must NOT be killed by first_turn_timeout — dsh turns
    legitimately run for minutes (issue #1: 394s in one turn). The
    timeout must measure silence, not elapsed time since the first
    event.
    """

    class _StreamingSpiSession:
        """Emits a TEXT event every 0.2s, finishes after ~1.6s."""

        async def events(self):
            for i in range(8):
                yield EventEnvelope(
                    seq=i + 1,
                    timestamp=0,
                    kind=EventKind.TEXT,
                    payload={"text": f"chunk-{i}"},
                )
                await asyncio.sleep(0.2)
            yield EventEnvelope(
                seq=9,
                timestamp=0,
                kind=EventKind.SESSION_COMPLETE,
                payload={"reason": "success"},
            )

    session = _agent_session(with_stop=False)

    await asyncio.wait_for(
        _runner()._process_events(
            _StreamingSpiSession(),
            session,
            {},
            None,
            None,
            None,
            None,
            timeouts=_timeouts(first_turn=1.0),
        ),
        timeout=5.0,
    )

    assert session.session_end_reason != "first_turn_timeout"
    assert session.status == "completed"


@pytest.mark.asyncio
async def test_diagnostics_refresh_during_event_loop() -> None:
    """The registry diagnostics (Turns/Tools/Output Chars)
    were only written once at run start — the dashboard showed 0/0 for
    the whole turn. The event loop must refresh them periodically and
    keep session.last_agent_event current.
    """
    from orchestratord.spi.events import EventEnvelope as _Env

    class _SlowStream:
        async def events(self):
            for i in range(10):
                yield _Env(
                    seq=i + 1,
                    timestamp=0,
                    kind=EventKind.TEXT,
                    payload={"text": f"c{i}"},
                )
                await asyncio.sleep(0.4)

    session = _agent_session(with_stop=False)
    calls: list = []

    await asyncio.wait_for(
        _runner()._process_events(
            _SlowStream(),
            session,
            {},
            None,
            None,
            None,
            None,
            timeouts=_timeouts(),
            diagnostics_callback=lambda s: calls.append(s),
        ),
        timeout=5.0,
    )

    assert len(calls) >= 2, (
        f"diagnostics must refresh during the run, got {len(calls)} call(s)"
    )
    assert session.last_agent_event, "last_agent_event must be recorded"
