"""Exercise the production adapter against an isolated local query runtime."""

from __future__ import annotations

import asyncio
import importlib.util
import os
import sys
import types

import psutil
import pytest
from orchestratord_clawcodex.backend import ClawcodexBackend

from orchestratord.spi.approval import ApprovalDecision
from orchestratord.spi.backend import SessionSpec
from orchestratord.spi.events import EventKind

RUNTIME = '''
import asyncio, os, subprocess, sys
from dataclasses import dataclass
from types import SimpleNamespace

class QueryConfig(SimpleNamespace):
    def __init__(self, **kwargs): super().__init__(**kwargs)
@dataclass
class TextDelta: content: str
@dataclass
class ToolCallEvent:
    tool_name: str
    params: dict
    tool_use_id: str = "tool"
@dataclass
class ToolResultEvent:
    tool_name: str
    result: dict
    tool_use_id: str = "tool"
@dataclass
class TurnComplete: turn: int
@dataclass
class PhaseComplete:
    phase: int
    turn_count: int
@dataclass
class SessionComplete: reason: str
@dataclass
class ApprovalRequestEvent:
    request_id: str
    call_id: str
    tool_name: str
    arguments: dict
    message: str

class QueryRunner:
    def __init__(self, config):
        self.config = config
        self.answer = asyncio.Event()
        self.decision = None
    def approve(self, request_id, decision):
        self.decision = decision.value
        self.answer.set()
        return request_id == "approval"
    def cancel_pending_approvals(self, message): self.answer.set()
    async def stream(self):
        if self.config.prompt == "failure":
            raise RuntimeError("native failure")
        if self.config.prompt == "boot-wait":
            import pathlib, time
            pathlib.Path(self.config.workspace + "/boot-ready").write_text("ready")
            time.sleep(60)
            pathlib.Path(self.config.workspace + "/boot-finished").write_text("finished")
        if self.config.prompt == "approval":
            yield ApprovalRequestEvent("approval", "tool", "Bash", {}, "Allow?")
            await self.answer.wait()
            yield TextDelta(self.decision or "cancelled")
        elif self.config.prompt == "heartbeat":
            child_code = "import time,pathlib; p=pathlib.Path(" + repr(self.config.workspace + "/heartbeat") + ");\\nwhile True: p.write_text(str(time.monotonic())); time.sleep(.02)"
            child = subprocess.Popen([sys.executable, "-c", child_code], start_new_session=True)
            yield ToolCallEvent("Bash", {"pid": child.pid, "runtime_pid": os.getpid()})
            try:
                await asyncio.sleep(60)
            finally:
                child.kill()
                child.wait()
        else:
            yield TextDelta(self.config.prompt)
        yield SessionComplete("success")
'''


@pytest.fixture
def local_runtime(tmp_path, monkeypatch):
    if os.name != "posix":
        pytest.skip("Local process suspension requires POSIX")
    try:
        psutil.pids()
    except PermissionError:
        pytest.skip("Process-table access is required")
    source = tmp_path / "runtime"
    api_path = source / "extensions" / "api"
    api_path.mkdir(parents=True)
    (api_path.parent / "__init__.py").write_text("")
    (api_path / "__init__.py").write_text("")
    query_path = api_path / "query.py"
    query_path.write_text(RUNTIME)
    monkeypatch.setenv("CLAWCODEX_SOURCE", str(source))
    # The baseline in-process adapter also sees this exact local runtime.
    extensions = types.ModuleType("extensions")
    extensions.__path__ = [str(api_path.parent)]
    api = types.ModuleType("extensions.api")
    api.__path__ = [str(api_path)]
    extensions.api = api
    spec = importlib.util.spec_from_file_location("extensions.api.query", query_path)
    query = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, "extensions", extensions)
    monkeypatch.setitem(sys.modules, "extensions.api", api)
    monkeypatch.setitem(sys.modules, "extensions.api.query", query)
    api.query = query
    spec.loader.exec_module(query)
    return tmp_path


async def wait_until(predicate):
    async with asyncio.timeout(4):
        while not predicate():
            await asyncio.sleep(.02)


@pytest.mark.asyncio
async def test_pause_resume_stop_controls_tools_without_pausing_daemon(local_runtime):
    session = ClawcodexBackend().create_session(SessionSpec(cwd=str(local_runtime)))
    child = None
    try:
        await session.send("heartbeat")
        stream = session.events()
        event = await asyncio.wait_for(anext(stream), 4)
        assert event.kind is EventKind.TOOL_CALL
        child = psutil.Process(event.payload["arguments"]["pid"])
        heartbeat = local_runtime / "heartbeat"
        await wait_until(heartbeat.exists)
        await asyncio.wait_for(session.pause(), 2)
        frozen = heartbeat.read_text()
        await asyncio.sleep(.15)
        assert heartbeat.read_text() == frozen, "A paused tool kept writing"
        assert child.status() == psutil.STATUS_STOPPED
        assert event.payload["arguments"]["runtime_pid"] != os.getpid()
        await session.resume()
        await wait_until(lambda: heartbeat.read_text() != frozen)
        await session.pause()
        await asyncio.wait_for(session.close(), 3)
        await wait_until(lambda: not child.is_running() or child.status() == psutil.STATUS_ZOMBIE)
        rest = [event async for event in stream]
        assert rest[-1].kind is EventKind.SESSION_COMPLETE
        assert rest[-1].payload["reason"] == "stopped"
        assert not any(event.kind is EventKind.ERROR for event in rest)
    finally:
        if child is not None and child.is_running():
            child.kill()
        await session.close()


@pytest.mark.asyncio
async def test_worker_three_turns_and_approval_roundtrip(local_runtime):
    backend = ClawcodexBackend()
    session = backend.create_session(SessionSpec(cwd=str(local_runtime)))
    sequences = []
    try:
        for prompt in ("first", "approval", "third"):
            await session.send(prompt)
            texts = []
            async for event in session.events():
                sequences.append(event.seq)
                if event.kind is EventKind.APPROVAL_REQUEST:
                    assert texts == []
                    await session.approve(event.payload["request_id"], ApprovalDecision.ALLOW)
                elif event.kind is EventKind.TEXT_DELTA:
                    texts.append(event.payload["text"])
            assert "".join(texts) == ("allow" if prompt == "approval" else prompt)
        assert sequences == sorted(set(sequences))
        assert backend.capabilities().pausable == session.capabilities.pausable
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_pause_does_not_affect_peer_session(local_runtime):
    peer_workspace = local_runtime / "peer"
    peer_workspace.mkdir()
    backend = ClawcodexBackend()
    session = backend.create_session(SessionSpec(cwd=str(local_runtime)))
    peer = backend.create_session(SessionSpec(cwd=str(peer_workspace)))
    children = []
    try:
        for instance in (session, peer):
            await instance.send("heartbeat")
            event = await asyncio.wait_for(anext(instance.events()), 4)
            children.append(psutil.Process(event.payload["arguments"]["pid"]))
        heartbeat = peer_workspace / "heartbeat"
        await wait_until(heartbeat.exists)
        await session.pause()
        before = heartbeat.read_text()
        await wait_until(lambda: heartbeat.read_text() != before)
        await session.close()
        before = heartbeat.read_text()
        await wait_until(lambda: heartbeat.read_text() != before)
    finally:
        await session.close()
        await peer.close()
        for child in children:
            if child.is_running():
                child.kill()


@pytest.mark.asyncio
async def test_stop_before_dispatch_cannot_launch_worker(local_runtime, monkeypatch):
    launches = []
    monkeypatch.setattr(asyncio, "create_subprocess_exec", lambda *args, **kwargs: launches.append(args))
    session = ClawcodexBackend().create_session(SessionSpec(cwd=str(local_runtime)))
    await session.send("never execute")
    await asyncio.wait_for(session.close(), 2)
    assert launches == []
    events = [event async for event in session.events()]
    assert events[-1].payload["reason"] == "stopped"
    with pytest.raises(RuntimeError, match="closed"):
        await session.send("late request")


@pytest.mark.asyncio
async def test_stop_reaches_blocked_native_initialization(local_runtime):
    session = ClawcodexBackend().create_session(SessionSpec(cwd=str(local_runtime)))
    try:
        await session.send("boot-wait")
        await wait_until((local_runtime / "boot-ready").exists)
        await asyncio.wait_for(session.pause(), 2)
        await asyncio.wait_for(session.close(), 3)
        assert not (local_runtime / "boot-finished").exists()
        events = [event async for event in session.events()]
        assert events[-1].payload["reason"] == "stopped"
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_lost_daemon_pipe_stops_blocked_worker(local_runtime):
    session = ClawcodexBackend().create_session(SessionSpec(cwd=str(local_runtime)))
    try:
        await session.send("boot-wait")
        await wait_until((local_runtime / "boot-ready").exists)
        # Fault injection at the transport seam: the daemon's pipe disappears.
        session._process.stdin.close()
        await asyncio.wait_for(session._process.wait(), 3)
        assert not (local_runtime / "boot-finished").exists()
        events = [event async for event in session.events()]
        assert events[-1].payload["reason"] == "worker_error"
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_native_failure_has_terminal_event_and_retry_keeps_unique_sequence(local_runtime):
    session = ClawcodexBackend().create_session(SessionSpec(cwd=str(local_runtime)))
    try:
        await session.send("failure")
        failed = [event async for event in session.events()]
        assert failed[-1].kind is EventKind.SESSION_COMPLETE
        assert failed[-1].payload["reason"] != "success"
        assert any(event.kind is EventKind.ERROR for event in failed)
        await session.send("recovered")
        recovered = [event async for event in session.events()]
        assert recovered[-1].payload["reason"] == "success"
        sequences = [event.seq for event in failed + recovered]
        assert sequences == sorted(set(sequences))
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_failed_process_launch_can_be_closed(local_runtime):
    session = ClawcodexBackend().create_session(SessionSpec(cwd=str(local_runtime / "absent")))
    await session.send("task")
    async with asyncio.timeout(3):
        events = [event async for event in session.events()]
        await session.close()
    assert events[-1].payload["reason"] == "worker_error"


@pytest.mark.asyncio
async def test_dispose_before_dispatch_cannot_launch_worker(local_runtime, monkeypatch):
    launches = []
    monkeypatch.setattr(asyncio, "create_subprocess_exec", lambda *args, **kwargs: launches.append(args))
    backend = ClawcodexBackend()
    session = backend.create_session(SessionSpec(cwd=str(local_runtime)))
    await session.send("never execute")
    backend.dispose()
    async with asyncio.timeout(2):
        events = [event async for event in session.events()]
        await session.close()
    assert launches == []
    assert events[-1].payload["reason"] == "stopped"


@pytest.mark.asyncio
async def test_approval_ack_budget_does_not_expire_while_worker_is_paused(local_runtime):
    session = ClawcodexBackend().create_session(SessionSpec(
        cwd=str(local_runtime), handshake_timeout_s=.5,
    ))
    approval = None
    try:
        await session.send("approval")
        stream = session.events()
        request = await asyncio.wait_for(anext(stream), 4)
        assert request.kind is EventKind.APPROVAL_REQUEST
        await session.pause()
        approval = asyncio.create_task(session.approve(request.payload["request_id"], ApprovalDecision.ALLOW))
        await asyncio.sleep(.7)
        assert not approval.done(), "Paused time consumed the control acknowledgement budget"
        await session.resume()
        await asyncio.wait_for(approval, 2)
        events = [event async for event in stream]
        assert events[-1].payload["reason"] == "success"
    finally:
        await session.close()
        if approval is not None:
            await asyncio.gather(approval, return_exceptions=True)
