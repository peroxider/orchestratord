"""Regression: backend preflight must not block the daemon event loop.

Historical defect (D4, issue #25): ``BackendRunner._run_with_backend``
invoked the synchronous ``_preflight_spec`` directly inside the event
loop.  The dsh backend's preflight scans an 8MB runtime executable for
the llm-pi-ai plugin (``cordis_gen.probe_llm_pi_ai_available``), so the
first registry-route run could stall the whole daemon for seconds —
every session, control command, and heartbeat froze.

Contract pinned here: while a backend's preflight is slow (simulated by
a ``time.sleep`` in the backend's ``preflight``), the event loop must
keep servicing concurrent work (a control-plane heartbeat) for the
whole window.  Without the fix the synchronous preflight monopolizes
the loop and the heartbeat misses the window entirely.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from orchestratord.backend_runner import BackendRunner
from orchestratord.control_socket import ControlCommand
from orchestratord.spi.backend import SessionSpec
from orchestratord.spi.capabilities import BackendCapabilities


class _SlowPreflightBackend:
    """Backend whose preflight simulates the 8MB runtime binary scan."""

    name = "slow"
    display_name = "Slow"

    def __init__(self, delay: float) -> None:
        self._delay = delay

    def preflight(self, spec: SessionSpec) -> None:
        # Simulates probe_llm_pi_ai_available() reading an 8MB runtime
        # executable — pure blocking I/O inside the SPI preflight.
        time.sleep(self._delay)

    def capabilities(self) -> BackendCapabilities:
        return BackendCapabilities()

    def create_session(self, spec: SessionSpec):
        class _SpiSession:
            async def send(self, text: str) -> None:
                pass

            async def close(self) -> None:
                pass

        return _SpiSession()


def _minimal_session() -> SimpleNamespace:
    queue: asyncio.Queue[ControlCommand] = asyncio.Queue()
    return SimpleNamespace(
        run_id="run-preflight-nonblock",
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
        backend_name=None,
        backend_session_id=None,
        conversation_id=None,
        issue=SimpleNamespace(id="test"),
        workspace=SimpleNamespace(path=Path("/tmp")),
        _user_prompt="test prompt",
        _runtime_tasks=None,
        started_at=None,
        last_agent_event=None,
        last_tool_name=None,
        _snapshot_backend=None,
        _snapshot_provider=None,
        _snapshot_model=None,
        debug_log_path=None,
        tool_events_path=None,
    )


def _minimal_runner(backend: _SlowPreflightBackend) -> BackendRunner:
    runner = object.__new__(BackendRunner)
    runner.backend = backend
    runner.agent_config = SimpleNamespace(
        permission_mode="default",
        provider=None,
        model=None,
        run_timeout_ms=60000,
        stall_timeout_ms=60000,
        stall_warn_ms=10000,
        first_turn_timeout_ms=0,
    )
    runner._start_control_socket = AsyncMock(return_value=False)
    runner._process_events = AsyncMock()
    runner._resolve_timeouts = lambda spec: {
        "total": 60.0,
        "handshake": 60.0,
        "first_turn": 60.0,
        "inactivity": 60.0,
        "idle_watchdog": 60.0,
    }
    return runner


@pytest.mark.asyncio
async def test_preflight_does_not_block_event_loop() -> None:
    """A 2s preflight must not stall the event loop's other work.

    The heartbeat runs for 1.5s of wall-clock time while the backend's
    preflight sleeps for 2s.  When the event loop stays responsive the
    heartbeat ticks every 50ms (~30 ticks); when the synchronous
    preflight monopolizes the loop, the heartbeat misses the whole
    window and exits with ~0 ticks.
    """
    runner = _minimal_runner(_SlowPreflightBackend(delay=2.0))
    session = _minimal_session()
    spec = SessionSpec(
        cwd="/tmp",
        provider="slow",
        resume_session_id=None,
        system_prompt="",
    )

    # Concurrent control-plane heartbeat: mimics the daemon draining
    # other sessions' control commands / heartbeats during this run's
    # startup.  The deadline is anchored to the test start (not to when
    # the heartbeat coroutine first gets scheduled), so a blocked event
    # loop makes the heartbeat miss the window entirely.
    ticks: list[float] = []
    deadline = time.monotonic() + 1.5

    async def control_plane_heartbeat() -> None:
        while time.monotonic() < deadline:
            ticks.append(time.monotonic())
            await asyncio.sleep(0.05)

    run_task = asyncio.create_task(
        runner._run_with_backend(session, spec, None, None, None, None)
    )
    heartbeat_task = asyncio.create_task(control_plane_heartbeat())

    await asyncio.wait_for(heartbeat_task, timeout=5.0)
    # Let the run finish the remaining preflight + session teardown.
    await asyncio.wait_for(run_task, timeout=10.0)

    # 1.5s of 50ms ticks ≈ 30 ticks on a free loop.  A blocked loop
    # would have ticked ~0 times inside the preflight window.
    assert len(ticks) >= 20, (
        f"control-plane heartbeat only ticked {len(ticks)}/30 during a 2s "
        "preflight — the event loop was blocked by synchronous preflight I/O"
    )
    assert session.status == "running"
