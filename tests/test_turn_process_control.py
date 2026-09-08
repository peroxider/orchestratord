"""TurnProcessControl — operator control for per-turn backends.

A per-turn backend (e.g. ``claude -p``) only has a live child process
*during* a turn. These tests pin the adapter's contract against real
OS processes: pause freezes the child (``/proc`` state T), resume
continues it, kill reaps it and still fires the queued stop command,
and between turns pause raises while kill degrades to stop-only.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from orchestratord.control_socket import ControlCommand
from orchestratord.process_control import TurnProcessControl


def _proc_state(pid: int) -> str:
    """State letter from /proc/<pid>/stat (field 3, after the comm)."""
    raw = Path(f"/proc/{pid}/stat").read_text()
    return raw[raw.rindex(")") + 2:].split()[0]


async def _spawn_sleeper() -> asyncio.subprocess.Process:
    return await asyncio.create_subprocess_exec(
        "sleep", "300",
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )


async def test_pause_freezes_live_child_then_resume_continues() -> None:
    proc = await _spawn_sleeper()
    try:
        control = TurnProcessControl(pid_provider=lambda: proc.pid)
        control.pause()
        assert _proc_state(proc.pid) == "T"
        control.resume()
        assert _proc_state(proc.pid) != "T"
    finally:
        proc.kill()
        await proc.wait()


async def test_pause_is_idempotent_while_frozen() -> None:
    proc = await _spawn_sleeper()
    try:
        control = TurnProcessControl(pid_provider=lambda: proc.pid)
        control.pause()
        control.pause()  # second call must not re-enter or raise
        assert _proc_state(proc.pid) == "T"
    finally:
        proc.kill()
        await proc.wait()


def test_pause_between_turns_raises() -> None:
    control = TurnProcessControl(pid_provider=lambda: None)
    with pytest.raises(RuntimeError, match="No live agent process"):
        control.pause()


async def test_kill_kills_child_and_enqueues_stop() -> None:
    proc = await _spawn_sleeper()
    stops: list[ControlCommand] = []
    control = TurnProcessControl(
        pid_provider=lambda: proc.pid,
        stop_command=lambda: stops.append(ControlCommand("stop")),
    )
    control.kill()
    await proc.wait()
    assert proc.returncode == -9  # SIGKILL
    assert stops == [ControlCommand("stop")]


def test_kill_between_turns_still_enqueues_stop() -> None:
    stops: list[ControlCommand] = []
    control = TurnProcessControl(
        pid_provider=lambda: None,
        stop_command=lambda: stops.append(ControlCommand("stop")),
    )
    control.kill()  # nothing to signal — must not raise, stop still sent
    assert stops == [ControlCommand("stop")]


async def test_kill_already_dead_child_is_silent_and_still_stops() -> None:
    proc = await _spawn_sleeper()
    proc.kill()
    await proc.wait()
    stops: list[ControlCommand] = []
    control = TurnProcessControl(
        pid_provider=lambda: proc.pid,
        stop_command=lambda: stops.append(ControlCommand("stop")),
    )
    control.kill()  # reaped PID must not raise
    assert stops == [ControlCommand("stop")]


async def test_stop_command_failure_does_not_mask_kill() -> None:
    proc = await _spawn_sleeper()

    def _boom() -> None:
        raise RuntimeError("queue closed")

    control = TurnProcessControl(pid_provider=lambda: proc.pid, stop_command=_boom)
    control.kill()
    await proc.wait()
    assert proc.returncode == -9
