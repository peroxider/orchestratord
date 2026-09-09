"""Local process-control checks using the real SDK, without a model provider."""

import asyncio
import os
import sys
from types import SimpleNamespace

import psutil
import pytest
from deepseek_harness.api import DeepSeekHarness, DeepSeekHarnessConfig
from orchestratord_dsh.session import DshSession

from orchestratord.spi.backend import SessionSpec
from orchestratord.spi.degradation import DegradingSession
from orchestratord.spi.events import EventKind


@pytest.fixture(autouse=True)
def require_process_table():
    if os.name != "posix":
        pytest.skip("Local suspension is only advertised on POSIX")
    try:
        psutil.pids()
    except PermissionError:
        pytest.skip("Process tree integration requires OS process-table access")


def local_harness(tmp_path, heartbeat, shutdown_marker=None):
    child = (
        "import time,pathlib; p=pathlib.Path(" + repr(str(heartbeat)) + "); "
        "\nwhile True: p.write_text(str(time.monotonic())); time.sleep(.03)"
    )
    parent = (
        "import subprocess,sys,time; "
        f"subprocess.Popen([sys.executable,'-c',{child!r}],start_new_session=True); "
    )
    if shutdown_marker is None:
        parent += "time.sleep(60)"
    else:
        parent += (
            "\nimport json,pathlib\n"
            "for line in sys.stdin:\n"
            " message=json.loads(line)\n"
            " if message['method']=='shutdown':\n"
            f"  pathlib.Path({str(shutdown_marker)!r}).write_text('flushed')\n"
            "  print(json.dumps({'jsonrpc':'2.0','id':message['id'],'result':{}}),flush=True)\n"
            "  break\n"
        )
    return DeepSeekHarness(
        DeepSeekHarnessConfig(
            cwd=str(tmp_path),
            dsh_home=str(tmp_path / "dsh-home"),
            initialize_timeout_seconds=5,
            shutdown_timeout_seconds=.1,
        ),
        _launch_args=[sys.executable, "-c", parent],
    )


async def wait_until(predicate):
    async with asyncio.timeout(3):
        while not predicate():
            await asyncio.sleep(.01)


def _is_gone_or_zombie(proc: psutil.Process) -> bool:
    """Return True if *proc* has exited or is a zombie.

    Treat psutil probing errors (NoSuchProcess, AccessDenied) as the
    process already having exited — these are TOCTOU races between the
    PID being reaped and the status check.
    """
    try:
        return not proc.is_running() or proc.status() == psutil.STATUS_ZOMBIE
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return True


@pytest.mark.asyncio
@pytest.mark.parametrize("initializing", [False, True])
async def test_pause_resume_stop_reaches_detached_tools(tmp_path, monkeypatch, initializing):
    heartbeat = tmp_path / "controlled"
    peer_heartbeat = tmp_path / "peer"
    harness = local_harness(tmp_path, heartbeat)
    peer = local_harness(tmp_path, peer_heartbeat)
    # For the active-turn case, bypass only JSON-RPC initialization. The real
    # SDK still spawns, owns, reads and closes its subprocess as in production.
    if not initializing:
        monkeypatch.setattr(harness.client, "initialize", lambda **kwargs: None)
    monkeypatch.setattr("deepseek_harness.api.DeepSeekHarness", lambda config: harness)
    session = DshSession(SessionSpec(cwd=str(tmp_path)))
    processes = []
    try:
        peer.client.start()
        await session.send("local process control test")
        await wait_until(lambda: heartbeat.exists() and peer_heartbeat.exists())
        root = psutil.Process(harness.client._proc.pid)
        processes = [root, *root.children(recursive=True)]
        assert len(processes) >= 2
        controlled = DegradingSession(session)
        await asyncio.wait_for(controlled.pause(), 1)
        frozen = heartbeat.read_text()
        peer_before = peer_heartbeat.read_text()
        await asyncio.sleep(.15)
        assert heartbeat.read_text() == frozen, "Tool progressed while paused"
        assert all(p.status() == psutil.STATUS_STOPPED for p in processes)
        assert peer_heartbeat.read_text() != peer_before, "Another session was paused"
        await controlled.resume()
        await wait_until(lambda: heartbeat.read_text() != frozen)
        await controlled.pause()
        await asyncio.wait_for(controlled.close(), 2)
        await wait_until(lambda: all(
            _is_gone_or_zombie(p) for p in processes
        ))
        assert session._turn_task.done(), "SDK worker outlived a successful close"
        events = [event async for event in session.events()]
        assert events[-1].payload["reason"] == "stopped"
        assert all(event.kind is not EventKind.ERROR for event in events)
        with pytest.raises(RuntimeError, match="closed"):
            harness.client.start()
        assert harness.client._proc is None, "SDK restarted after operator stop"
    finally:
        # Clean up only processes started by this test, including on a red run.
        for instance in (harness, peer):
            proc = instance.client._proc
            if proc is not None:
                try:
                    root = psutil.Process(proc.pid)
                    for process in [*reversed(root.children(recursive=True)), root]:
                        process.kill()
                except psutil.NoSuchProcess:
                    pass
            instance.close()
        await session.close()


@pytest.mark.asyncio
async def test_stop_before_dispatch_cannot_start_sdk(tmp_path, monkeypatch):
    starts = []
    monkeypatch.setattr(
        "deepseek_harness.api.DeepSeekHarness",
        lambda config: starts.append(config),
    )
    session = DshSession(SessionSpec(cwd=str(tmp_path)))
    await session.send("task")
    await session.close()
    assert session._turn_task.done()
    assert starts == []


@pytest.mark.asyncio
async def test_completed_session_flushes_before_cleaning_up_tools(tmp_path, monkeypatch):
    heartbeat = tmp_path / "heartbeat"
    flushed = tmp_path / "flushed"
    harness = local_harness(tmp_path, heartbeat, flushed)
    monkeypatch.setattr(harness.client, "initialize", lambda **kwargs: None)
    monkeypatch.setattr(harness, "run", lambda *args, **kwargs: SimpleNamespace(finish_reason="success"))
    session = DshSession(SessionSpec(cwd=str(tmp_path)), harness_factory=lambda: harness)
    processes = []
    try:
        await session.send("completed task")
        _ = [event async for event in session.events()]
        await wait_until(heartbeat.exists)
        root = psutil.Process(harness.client._proc.pid)
        processes = [root, *root.children(recursive=True)]
        await session.close()
        # Deterministic sync: wait for the flush marker instead of relying
        # on the relative timing between the worker thread and the cleanup
        # path.  The 3s timeout ensures the test always fails fast if the
        # shutdown flush never arrives (e.g. process killed before flush).
        async with asyncio.timeout(3):
            while flushed.read_text() != "flushed":
                await asyncio.sleep(.01)
        assert flushed.read_text() == "flushed", "Normal completion lost SDK shutdown flush"
        await wait_until(lambda: all(
            _is_gone_or_zombie(p) for p in processes
        ))
    finally:
        for process in reversed(processes):
            try:
                process.kill()
            except psutil.NoSuchProcess:
                pass
        await session.close()
