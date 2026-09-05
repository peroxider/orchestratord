"""Run the installed AgentSDK query stack with only the remote provider replaced."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import psutil
import pytest
from orchestratord_clawcodex.backend import ClawcodexBackend

from orchestratord.spi.approval import ApprovalDecision
from orchestratord.spi.backend import SessionSpec
from orchestratord.spi.events import EventKind
from orchestratord.spi.session import ResumeStatus

BOOTSTRAP = '''
import os, socket
from pathlib import Path
original_connect = socket.socket.connect
def local_connect(connection, address):
    if connection.family in (socket.AF_INET, socket.AF_INET6):
        raise AssertionError("External network is forbidden in this local provider test")
    return original_connect(connection, address)
socket.socket.connect = local_connect
socket.socket.connect_ex = local_connect
from clawcodex_ext.providers.base import ChatResponse
import clawcodex_ext.entrypoints.headless as headless

class LocalProvider:
    def __init__(self, api_key, base_url=None, model=None, **kwargs):
        self.model = model or "local-test"
        self.calls = 0
    def chat(self, messages, tools=None, **kwargs):
        self.calls += 1
        if os.environ.get("TEST_NATIVE_TOOL"):
            marker = Path(os.environ["TEST_NATIVE_TOOL"])
            if self.calls == 1:
                import shlex, sys
                command = "printf approved > " + shlex.quote(str(marker))
                if os.environ.get("TEST_HOLD_TOOL"):
                    code = "import pathlib,time; p=pathlib.Path(" + repr(str(marker)) + ");\\nwhile True: p.write_text(str(time.monotonic())); time.sleep(.03)"
                    command = shlex.quote(sys.executable) + " -c " + shlex.quote(code)
                return ChatResponse(content="before tool", model=self.model,
                    usage={"input_tokens": 7, "output_tokens": 2}, finish_reason="tool_use",
                    tool_uses=[{"id": "native-write", "name": "Bash", "input": {
                        "command": command,
                        "description": "Write an isolated approval-test marker",
                    }}])
            assert marker.read_text() == "approved", "Native tool did not run"
            return ChatResponse(content="after tool", model=self.model,
                                usage={"input_tokens": 7, "output_tokens": 2}, finish_reason="end_turn")
        if os.environ.get("EXPECT_READ_ONLY") == "1":
            names = {tool.get("name") or tool.get("function", {}).get("name") for tool in tools or []}
            assert names == {"Read"}, "Native tool filtering was not applied: " + str(names)
        current = str(messages[-1])
        history = str(messages)
        if "stage-three" in current:
            assert "answer-one" in history and "answer-two" in history, "History was lost"
            text = "answer-three"
        elif "stage-two" in current:
            assert "answer-one" in history, "Followup did not resume the saved session"
            text = "answer-two"
        else:
            text = "answer-one"
        return ChatResponse(content=text, model=self.model,
                            usage={"input_tokens": 7, "output_tokens": 2}, finish_reason="end_turn")
    async def chat_async(self, messages, tools=None, **kwargs):
        return self.chat(messages, tools, **kwargs)
    def chat_stream(self, *args, **kwargs): raise NotImplementedError

headless.get_provider_class = lambda name: LocalProvider
headless.get_provider_config = lambda name: {"api_key": "test-only-key", "default_model": "local-test"}
headless.get_default_provider = lambda: "anthropic"
'''


@pytest.mark.asyncio
@pytest.mark.parametrize("filter_tools", [False, True])
async def test_installed_native_worker_preserves_three_turn_history(tmp_path, filter_tools):
    query = pytest.importorskip("extensions.api.query")
    native_source = Path(query.__file__).resolve().parents[2]
    bootstrap = tmp_path / "bootstrap"
    bootstrap.mkdir()
    (bootstrap / "sitecustomize.py").write_text(BOOTSTRAP)
    state = tmp_path / "state"
    env = {
        "PYTHONPATH": str(bootstrap),
        "CLAWCODEX_SOURCE": str(native_source),
        "CLAWCODEX_HOME": str(state),
        "CLAWCODEX_CONFIG_DIR": str(state),
        "CLAWCODEX_SESSIONS_DIR": str(state / "sessions"),
        "CLAW_TELEMETRY_STORAGE_DIR": str(state / "telemetry"),
        "CLAW_TELEMETRY_REPORTING_ENABLED": "0",
        "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
        "EXPECT_READ_ONLY": "1" if filter_tools else "0",
    }
    tool_options = {"tools_allow": ["Read", "Bash"], "tools_deny": ["Bash"]} if filter_tools else {}
    session = ClawcodexBackend().create_session(SessionSpec(cwd=str(tmp_path), env=env, model="local-test", **tool_options))
    native_id = None
    try:
        for number in ("one", "two", "three"):
            await session.send("stage-" + number)
            async with asyncio.timeout(15):
                events = [event async for event in session.events()]
            errors = [e.payload for e in events if e.kind is EventKind.ERROR]
            assert not errors, errors
            text = "".join(e.payload["text"] for e in events if e.kind is EventKind.TEXT_DELTA)
            assert text == "answer-" + number
            assert events[-1].payload["reason"] == "success"
            assert events[-1].payload["usage"]["input_tokens"] == 7
            if native_id is None:
                native_id = session.session_id
            assert session.session_id == native_id
        assert (state / "sessions" / native_id).is_dir()
    finally:
        await session.close()
    restored = ClawcodexBackend().create_session(SessionSpec(
        cwd=str(tmp_path), env=env, model="local-test", resume_session_id=native_id,
        **tool_options,
    ))
    try:
        assert await restored.probe_resume() is ResumeStatus.RESUMED
        await restored.send("stage-three")
        async with asyncio.timeout(15):
            events = [event async for event in restored.events()]
        assert events[-1].payload["reason"] == "success"
        assert restored.session_id == native_id
        assert "".join(e.payload["text"] for e in events if e.kind is EventKind.TEXT_DELTA) == "answer-three"
    finally:
        await restored.close()


@pytest.mark.asyncio
async def test_native_bash_is_really_suspended_resumed_and_stopped(tmp_path):
    if os.name != "posix":
        pytest.skip("Local suspension is only supported on POSIX")
    query = pytest.importorskip("extensions.api.query")
    bootstrap = tmp_path / "bootstrap"
    bootstrap.mkdir()
    (bootstrap / "sitecustomize.py").write_text(BOOTSTRAP)
    state = tmp_path / "state"
    heartbeat = tmp_path / "heartbeat"
    env = {
        "PYTHONPATH": str(bootstrap),
        "CLAWCODEX_SOURCE": str(Path(query.__file__).resolve().parents[2]),
        "CLAWCODEX_HOME": str(state), "CLAWCODEX_CONFIG_DIR": str(state),
        "CLAWCODEX_SESSIONS_DIR": str(state / "sessions"),
        "CLAW_TELEMETRY_STORAGE_DIR": str(state / "telemetry"),
        "CLAW_TELEMETRY_REPORTING_ENABLED": "0",
        "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
        "TEST_NATIVE_TOOL": str(heartbeat), "TEST_HOLD_TOOL": "1",
    }
    session = ClawcodexBackend().create_session(SessionSpec(
        cwd=str(tmp_path), env=env, model="local-test", permission_mode="default", tools_allow=["Bash"],
    ))
    events = []

    async def consume():
        async for event in session.events():
            events.append(event)
            if event.kind is EventKind.APPROVAL_REQUEST:
                await session.approve(event.payload["request_id"], ApprovalDecision.ALLOW)

    consumer = None
    try:
        await session.send("Exercise a running native tool")
        consumer = asyncio.create_task(consume())
        async with asyncio.timeout(15):
            while not heartbeat.exists():
                if consumer.done():
                    pytest.fail("Native tool did not start: " + str([e.payload for e in events]))
                await asyncio.sleep(.03)
        processes = [session._tree.root, *session._tree.root.children(recursive=True)]
        assert len(processes) >= 2
        await session.pause()
        frozen = heartbeat.read_text()
        await asyncio.sleep(.2)
        assert heartbeat.read_text() == frozen
        assert all(process.status() == psutil.STATUS_STOPPED for process in processes)
        await session.resume()
        async with asyncio.timeout(3):
            while heartbeat.read_text() == frozen:
                await asyncio.sleep(.03)
        await session.pause()
        await asyncio.wait_for(session.close(), 4)
        await asyncio.wait_for(consumer, 3)
        assert events[-1].payload["reason"] == "stopped"
        async with asyncio.timeout(3):
            while any(p.is_running() and p.status() != psutil.STATUS_ZOMBIE for p in processes):
                await asyncio.sleep(.03)
    finally:
        await session.close()
        if consumer is not None:
            await asyncio.gather(consumer, return_exceptions=True)


@pytest.mark.asyncio
async def test_native_bash_permission_text_and_result_cross_worker(tmp_path):
    query = pytest.importorskip("extensions.api.query")
    bootstrap = tmp_path / "bootstrap"
    bootstrap.mkdir()
    (bootstrap / "sitecustomize.py").write_text(BOOTSTRAP)
    state = tmp_path / "state"
    marker = tmp_path / "approved.txt"
    env = {
        "PYTHONPATH": str(bootstrap),
        "CLAWCODEX_SOURCE": str(Path(query.__file__).resolve().parents[2]),
        "CLAWCODEX_HOME": str(state), "CLAWCODEX_CONFIG_DIR": str(state),
        "CLAWCODEX_SESSIONS_DIR": str(state / "sessions"),
        "CLAW_TELEMETRY_STORAGE_DIR": str(state / "telemetry"),
        "CLAW_TELEMETRY_REPORTING_ENABLED": "0",
        "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
        "TEST_NATIVE_TOOL": str(marker),
    }
    session = ClawcodexBackend().create_session(SessionSpec(
        cwd=str(tmp_path), env=env, model="local-test", permission_mode="default", tools_allow=["Bash"],
    ))
    events = []
    try:
        await session.send("Exercise the native tool")
        async with asyncio.timeout(15):
            async for event in session.events():
                events.append(event)
                if event.kind is EventKind.APPROVAL_REQUEST:
                    assert not marker.exists(), "Tool executed before approval"
                    await session.approve(event.payload["request_id"], ApprovalDecision.ALLOW)
        assert not [event.payload for event in events if event.kind is EventKind.ERROR]
        assert marker.read_text() == "approved"
        assert sum(event.kind is EventKind.APPROVAL_REQUEST for event in events) == 1
        assert sum(event.kind is EventKind.TOOL_CALL for event in events) == 1
        assert sum(event.kind is EventKind.TOOL_RESULT for event in events) == 1
        assert "".join(e.payload["text"] for e in events if e.kind is EventKind.TEXT_DELTA) == "before toolafter tool"
        assert sum(e.payload["turn_delta"] for e in events if e.kind is EventKind.TURN_COMPLETE) == 2
        assert events[-1].payload["reason"] == "success"
        assert events[-1].payload["usage"]["input_tokens"] == 14
    finally:
        await session.close()
