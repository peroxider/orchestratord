"""Regression tests for O9 control-plane fixes (issue #31).

Five sub-items, each with at least one targeted assertion that would
fail without the accompanying source fix.

1. Control channel auth (SO_PEERCRED via ControlSocket._peer_authorized)
2. PID recycling false positive (_is_pid_alive cmdline cross-check)
3. Gateway control result timeout too short (_wait_gateway_control_result)
4. Retry clear before launch early-exit loss (_process_retry_queue)
5. Legacy asyncio.get_event_loop → get_running_loop (_check_retry_rate_limit)
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

# ---------------------------------------------------------------------------
# Sub-item 1: Control channel auth — SO_PEERCRED
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_control_socket_rejects_peer_with_different_uid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Unix socket peer whose UID does not match the daemon's UID must
    be rejected before it can send commands.

    We monkeypatch os.getuid() to return a different value so the
    SO_PEERCRED UID check inside _peer_authorized fails.
    """
    from orchestratord.control_socket import ControlSocket

    sock_path = tmp_path / ".run_control" / "test-auth.sock"
    sock_path.parent.mkdir(parents=True)
    control = ControlSocket(sock_path)
    await control.start()
    try:
        # Connect as a real client — the server will check SO_PEERCRED.
        # The real peer's UID is os.getuid(); we make the daemon-side
        # check expect a different UID, so it rejects.
        monkeypatch.setattr(os, "getuid", lambda: 99999)
        _reader, writer = await asyncio.open_unix_connection(str(sock_path))
        try:
            # Send a command — it should never be enqueued because the
            # server _on_client_connected rejected the connection.
            from orchestratord.control_socket import send_cmd

            await send_cmd(writer, "stop")
            # Yield control so the server's read-loop task can run
            await asyncio.sleep(0.1)
            # The command queue must remain empty — the connection was
            # rejected before _read_loop started.
            assert control._command_queue.empty(), (
                "Rejected peer must not have its commands enqueued"
            )
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionResetError, BrokenPipeError, OSError):
                # The server rejects by closing the socket — the reset
                # is exactly the behavior we want to observe.
                pass
    finally:
        await control.stop()


# ---------------------------------------------------------------------------
# Sub-item 2: PID recycling — _is_pid_alive cmdline cross-check
# ---------------------------------------------------------------------------


def test_is_pid_alive_accepts_orchestratord_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A PID whose cmdline contains 'orchestratord' is alive."""
    from orchestratord.cli.server import _is_pid_alive

    monkeypatch.setattr(os, "kill", lambda pid, sig: None)

    def _fake_cmdline(_self: Path) -> bytes:
        return b"/home/user/.venv/bin/python\x00-m\x00orchestratord\x00..."

    monkeypatch.setattr(Path, "read_bytes", _fake_cmdline)
    assert _is_pid_alive(42) is True


def test_is_pid_alive_recycles_rejects_unrelated_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A PID whose cmdline does NOT contain 'orchestratord' is treated as
    dead (recycled PID scenario)."""
    from orchestratord.cli.server import _is_pid_alive

    monkeypatch.setattr(os, "kill", lambda pid, sig: None)

    def _fake_cmdline(_self: Path) -> bytes:
        return b"/usr/bin/sleep\x00100"

    monkeypatch.setattr(Path, "read_bytes", _fake_cmdline)
    assert _is_pid_alive(42) is False


def test_is_pid_alive_falls_back_when_proc_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """On non-Linux platforms where /proc does not exist, the cmdline
    read raises OSError and the function falls back to the plain
    liveness test (signal 0)."""
    from orchestratord.cli.server import _is_pid_alive

    monkeypatch.setattr(os, "kill", lambda pid, sig: None)

    def _raise_oserror(_self: Path) -> bytes:
        raise OSError("no /proc on this platform")

    monkeypatch.setattr(Path, "read_bytes", _raise_oserror)
    assert _is_pid_alive(42) is True


def test_is_pid_alive_dead_pid_returns_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A dead PID is always False regardless of cmdline fallback."""
    from orchestratord.cli.server import _is_pid_alive

    monkeypatch.setattr(os, "kill", lambda pid, sig: (_ for _ in ()).throw(ProcessLookupError()))
    assert _is_pid_alive(42) is False


# ---------------------------------------------------------------------------
# Sub-item 3: Gateway control timeout — _wait_gateway_control_result
# ---------------------------------------------------------------------------


def test_wait_gateway_control_result_default_timeout_covers_poll_interval() -> None:
    """The default timeout must be >= 30 s (one poll interval) so the
    success path is reachable."""
    import inspect

    from orchestratord.cli.server import _wait_gateway_control_result

    sig = inspect.signature(_wait_gateway_control_result)
    default = sig.parameters["timeout_seconds"].default
    assert default >= 30.0, (
        f"gateway control timeout must be >= 30s (poll interval), got {default}"
    )


@pytest.mark.asyncio
async def test_wait_gateway_control_result_picks_up_response_file(
    tmp_path: Path,
) -> None:
    """A response file written within the timeout window is returned."""
    from orchestratord.cli.server import _wait_gateway_control_result

    response = tmp_path / "test_gateway_connect.result.json"

    # Write the response after a short delay; run the blocking wait in a
    # worker thread so the delayed write can execute on the event loop.
    async def _write_later() -> None:
        await asyncio.sleep(0.05)
        response.write_text(
            json.dumps({"ok": True, "message": "connected"}), encoding="utf-8"
        )

    asyncio.create_task(_write_later())
    result = await asyncio.to_thread(_wait_gateway_control_result, response, 1.0)
    assert result is not None
    assert result["ok"] is True
    assert result["message"] == "connected"


# ---------------------------------------------------------------------------
# Sub-item 4: Retry clear before launch early-exit loss
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_retry_launch_gated_restores_plan(tmp_path: Path) -> None:
    """When _launch_issue early-exits (does not add to running), the
    retry plan must be re-queued, not silently dropped."""
    from orchestratord.issue_registry import IssueRegistry
    from orchestratord.orchestrator import Orchestrator, OrchestratorState
    from orchestratord.session_state import RetryItem

    orch = object.__new__(Orchestrator)
    orch._state = OrchestratorState()
    orch._registry = IssueRegistry(tmp_path / "registry.json")
    orch._registry.register(issue_id="1", issue_identifier="ISSUE-1")
    orch.workflow = SimpleNamespace(
        agent=SimpleNamespace(
            max_retry_attempts=3,
            max_retry_backoff_ms=60_000,
            max_turns_retry_delay_ms=60_000,
        )
    )
    orch._state.max_concurrent_agents = 2
    orch.tracker = SimpleNamespace(
        active_states=["open"],
        fetch_issue_states_by_ids=_fetch_ok,
    )

    # Simulate a _launch_issue that early-exits (does not enter running)
    gated = False

    async def _gated_launch(issue) -> None:
        nonlocal gated
        gated = True
        # Early exit — no self._state.running set

    orch._launch_issue = _gated_launch  # type: ignore[method-assign]

    item = RetryItem(
        issue_id="1",
        attempt=1,
        delay_seconds=0.0,
        identifier="ISSUE-1",
        scheduled_at=0.0,
    )
    orch._state.retry_queue = [item]

    await orch._process_retry_queue()

    assert gated is True, "launch gate must have been reached"
    assert len(orch._state.retry_queue) == 1, (
        "gated retry must be re-queued, not silently lost"
    )
    assert orch._state.retry_queue[0].issue_id == "1"


@pytest.mark.asyncio
async def test_retry_launch_success_clears_plan(tmp_path: Path) -> None:
    """When _launch_issue succeeds (issue enters running), the retry
    plan is cleared and the queue is drained."""
    from orchestratord.issue_registry import IssueRegistry
    from orchestratord.orchestrator import Orchestrator, OrchestratorState
    from orchestratord.session_state import RetryItem

    orch = object.__new__(Orchestrator)
    orch._state = OrchestratorState()
    orch._registry = IssueRegistry(tmp_path / "registry.json")
    orch._registry.register(issue_id="1", issue_identifier="ISSUE-1")
    orch.workflow = SimpleNamespace(
        agent=SimpleNamespace(
            max_retry_attempts=3,
            max_retry_backoff_ms=60_000,
            max_turns_retry_delay_ms=60_000,
        )
    )
    orch._state.max_concurrent_agents = 2
    orch.tracker = SimpleNamespace(
        active_states=["open"],
        fetch_issue_states_by_ids=_fetch_ok,
    )

    launched = False

    async def _success_launch(issue) -> None:
        nonlocal launched
        launched = True
        # Simulate real _launch_issue: set running map
        orch._state.running["1"] = SimpleNamespace(issue=issue)

    orch._launch_issue = _success_launch  # type: ignore[method-assign]
    # Preserve the next_retry_at so we can verify it's cleared
    record = orch._registry.get("1")
    assert record is not None
    record.next_retry_at = 100.0
    orch._registry._save()

    item = RetryItem(
        issue_id="1",
        attempt=1,
        delay_seconds=0.0,
        identifier="ISSUE-1",
        scheduled_at=0.0,
    )
    orch._state.retry_queue = [item]

    await orch._process_retry_queue()

    assert launched is True
    assert orch._state.retry_queue == [], "successful launch must drain the queue"
    assert record.next_retry_at is None, "plan must be cleared on success"


# ---------------------------------------------------------------------------
# Sub-item 5: Legacy asyncio.get_event_loop → get_running_loop
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_check_retry_rate_limit_uses_running_loop(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """When called from within a running event loop, _check_retry_rate_limit
    must use get_running_loop (not get_event_loop) and schedule the
    rejection via create_task.

    We verify this by monkeypatch-watching asyncio.get_running_loop and
    ensuring the rejection path is taken.
    """
    from orchestratord.issue_registry import IssueRegistry
    from orchestratord.orchestrator import Orchestrator, OrchestratorState

    orch = object.__new__(Orchestrator)
    orch._state = OrchestratorState()
    orch._registry = IssueRegistry(tmp_path / "registry.json")
    orch._registry.register(issue_id="1", issue_identifier="ISSUE-1")
    orch.workflow = SimpleNamespace(
        agent=SimpleNamespace(
            max_retries_per_issue=3,
        )
    )
    orch.tracker = SimpleNamespace(
        create_comment=lambda _i, _b: 42,
        add_label=lambda _i, _l: None,
    )

    # Set retry_count >= max_retries_per_issue to hit the reject path
    record = orch._registry.get("1")
    assert record is not None
    record.retry_count = 3
    orch._registry._save()

    # Track which loop API was used
    called_get_running_loop = False
    original_get_running_loop = asyncio.get_running_loop

    def _track_get_running_loop():
        nonlocal called_get_running_loop
        called_get_running_loop = True
        return original_get_running_loop()

    monkeypatch.setattr(asyncio, "get_running_loop", _track_get_running_loop)

    # Also track _post_retry_rejection calls
    rejection_called = False

    async def _track_rejection(issue_id, current, max_retries):
        nonlocal rejection_called
        rejection_called = True

    monkeypatch.setattr(orch, "_post_retry_rejection", _track_rejection)

    issue = SimpleNamespace(id="1")
    result = orch._check_retry_rate_limit(issue, force=False)

    assert result is False, "rate limit hit → must return False"
    assert called_get_running_loop is True, (
        "get_running_loop must be used (not get_event_loop)"
    )
    # The rejection is scheduled as a task; give it a moment to run
    await asyncio.sleep(0.05)
    assert rejection_called is True, "retry rejection must be dispatched"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _fetch_ok(ids):
    return {i: SimpleNamespace(id=i, state="open") for i in ids}