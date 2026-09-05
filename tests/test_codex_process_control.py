"""Real local subprocess checks, without a provider or the Codex binary."""

import asyncio
import sys

import psutil
import pytest
from orchestratord_codex.session import CodexSession

from orchestratord.spi.backend import SessionSpec
from orchestratord.spi.degradation import DegradingSession


@pytest.fixture(autouse=True)
def require_process_table():
    try:
        psutil.pids()
    except PermissionError:
        pytest.skip("Process tree integration requires OS process-table access")


@pytest.mark.asyncio
async def test_pause_resume_stop_controls_detached_tool_process(tmp_path, monkeypatch):
    heartbeat = tmp_path / "heartbeat"
    child = (
        "import time,pathlib; p=pathlib.Path(" + repr(str(heartbeat)) + "); "
        "\nwhile True: p.write_text(str(time.monotonic())); time.sleep(.03)"
    )
    parent = (
        "import subprocess,sys,time; "
        f"subprocess.Popen([sys.executable,'-c',{child!r}],start_new_session=True); "
        "time.sleep(60)"
    )
    session = CodexSession(SessionSpec(cwd=str(tmp_path), total_timeout_s=20))
    monkeypatch.setattr(session, "_build_argv", lambda text: ([sys.executable, "-c", parent], None))
    processes = []
    try:
        await session.send("test")
        for _ in range(200):
            if heartbeat.exists():
                break
            await asyncio.sleep(.01)
        assert heartbeat.exists(), "The real tool process never started"
        root = psutil.Process(session._process.pid)
        processes = [root, *root.children(recursive=True)]
        assert len(processes) >= 2
        controlled = DegradingSession(session)
        await controlled.pause()
        frozen = heartbeat.read_text()
        await asyncio.sleep(.2)
        assert heartbeat.read_text() == frozen, "Tool progressed while paused"
        assert all(p.status() == psutil.STATUS_STOPPED for p in processes)
        await controlled.resume()
        await asyncio.sleep(.15)
        assert heartbeat.read_text() != frozen, "Tool did not resume"
        await controlled.pause()
        await controlled.close()
        await asyncio.sleep(.1)
        assert not any(p.is_running() and p.status() != psutil.STATUS_ZOMBIE for p in processes)
    finally:
        for process in reversed(processes):
            try:
                process.kill()
            except psutil.NoSuchProcess:
                pass
        await session.close()


@pytest.mark.asyncio
async def test_paused_time_does_not_consume_backend_timeout(tmp_path, monkeypatch):
    session = CodexSession(SessionSpec(cwd=str(tmp_path), total_timeout_s=.25))
    monkeypatch.setattr(session, "_build_argv", lambda text: ([sys.executable, "-c", "import time; time.sleep(60)"], None))
    try:
        await session.send("test")
        for _ in range(100):
            if session._process is not None:
                break
            await asyncio.sleep(.005)
        await session.pause()
        await asyncio.sleep(.4)
        assert not session._run_task.done()
        await session.resume()
        await asyncio.wait_for(session._run_task, 2)
        events = [event async for event in session.events()]
        assert any(e.payload.get("code") == "codex_timeout" for e in events)
    finally:
        await session.close()
