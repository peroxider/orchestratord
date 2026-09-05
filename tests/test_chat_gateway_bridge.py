"""Tests for chat gateway bridge (Phase C)."""

from __future__ import annotations

import asyncio
import json
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase

from orchestratord.backend_runner import BackendRunner
from orchestratord.chat_gateway import ChatGateway, _truncate_tool_result
from orchestratord.control_socket import ControlSocket, send_cmd
from orchestratord.runner_utils import (
    _broadcast_to_socket,
    _transcript_message_from_frame,
)
from orchestratord.spi.events import EventEnvelope, EventKind


class TestTruncation(IsolatedAsyncioTestCase):
    """Tests for ToolResult truncation."""

    def test_short_result_not_truncated(self):
        frame = {
            "type": "ToolResultEvent",
            "data": {
                "tool_name": "grep",
                "result": {"output": "short output"},
            },
        }
        result = _truncate_tool_result(frame)
        output = result["data"]["result"]["output"]
        self.assertEqual(output, "short output")
        self.assertNotIn("truncated", result["data"]["result"])

    def test_long_result_truncated(self):
        frame = {
            "type": "ToolResultEvent",
            "data": {
                "tool_name": "grep",
                "result": {"output": "x" * 5000},
            },
        }
        result = _truncate_tool_result(frame)
        output = result["data"]["result"]["output"]
        self.assertLess(len(output), 5000)
        self.assertTrue(result["data"]["result"].get("truncated"))

    def test_non_tool_result_unchanged(self):
        frame = {"type": "TextDelta", "data": {"content": "hello" * 1000}}
        result = _truncate_tool_result(frame)
        self.assertEqual(result["data"]["content"], "hello" * 1000)


@__import__("pytest").mark.asyncio
async def test_spi_text_delta_is_broadcast_in_chat_vocabulary():
    class _Socket:
        def __init__(self) -> None:
            self.frames: list[dict] = []

        async def send_event(self, frame: dict) -> None:
            self.frames.append(frame)

    socket = _Socket()
    session = type("Session", (), {"control_socket": socket})()
    event = EventEnvelope(
        seq=1, timestamp=time.time(), kind=EventKind.TEXT_DELTA,
        payload={"delta": "hello"},
    )
    await _broadcast_to_socket(session, event)
    assert socket.frames == [{"type": "TextDelta", "data": {"content": "hello"}}]


@__import__("pytest").mark.asyncio
async def test_spi_lifecycle_is_broadcast_in_chat_vocabulary():
    class _Socket:
        def __init__(self) -> None:
            self.frames: list[dict] = []

        async def send_event(self, frame: dict) -> None:
            self.frames.append(frame)

    socket = _Socket()
    session = type("Session", (), {"control_socket": socket})()
    event = EventEnvelope(
        seq=2, timestamp=time.time(), kind=EventKind.SESSION_COMPLETE,
        payload={"reason": "success"},
    )
    await _broadcast_to_socket(session, event)
    assert socket.frames == [{"type": "SessionComplete", "data": {"reason": "success"}}]


@__import__("pytest").mark.asyncio
async def test_spi_error_is_broadcast_in_chat_vocabulary():
    class _Socket:
        def __init__(self) -> None:
            self.frames: list[dict] = []

        async def send_event(self, frame: dict) -> None:
            self.frames.append(frame)

    socket = _Socket()
    session = type("Session", (), {"control_socket": socket})()
    event = EventEnvelope(
        seq=3,
        timestamp=time.time(),
        kind=EventKind.ERROR,
        payload={"code": "codex_spawn_error", "message": "codex not found"},
    )
    await _broadcast_to_socket(session, event)
    assert socket.frames == [
        {
            "type": "Error",
            "data": {"code": "codex_spawn_error", "message": "codex not found"},
        }
    ]


def test_error_frame_remains_visible_in_replayed_transcript() -> None:
    message = _transcript_message_from_frame(
        {
            "type": "Error",
            "data": {
                "code": "codex_spawn_error",
                "message": "codex not found",
            },
        }
    )

    assert message["role"] == "system"
    assert message["content"] == [
        {"type": "text", "text": "codex not found"}
    ]
    assert message["code"] == "codex_spawn_error"


@__import__("pytest").mark.asyncio
async def test_long_unix_control_path_falls_back_to_loopback_tcp(
    tmp_path: Path,
) -> None:
    """Long macOS workspace paths must not silently disable live controls."""
    long_workspace = tmp_path / ("workspace-" + "x" * 96)
    sock_path = long_workspace / ".run_control" / "run-long.sock"
    assert len(str(sock_path).encode()) > 104

    control = ControlSocket(sock_path)
    await control.start()
    writer = None
    try:
        assert control.endpoint.startswith("tcp://127.0.0.1:")
        port = int(control.endpoint.rsplit(":", 1)[1])
        _reader, writer = await asyncio.open_connection("127.0.0.1", port)
        await send_cmd(writer, "pause")
        command = await asyncio.wait_for(
            anext(control.poll_commands()),
            timeout=1.0,
        )
        assert command.cmd == "pause"
    finally:
        if writer is not None:
            writer.close()
            await writer.wait_closed()
        await control.stop()


@__import__("pytest").mark.asyncio
async def test_backend_runner_publishes_tcp_fallback_for_dashboard_discovery(
    tmp_path: Path,
) -> None:
    long_workspace = tmp_path / ("workspace-" + "x" * 96)
    session = SimpleNamespace(
        control_socket=None,
        control_socket_path=None,
        run_id="run-long",
        workspace=SimpleNamespace(path=long_workspace),
    )

    owns_socket = await BackendRunner._start_control_socket(session)
    try:
        assert owns_socket is True
        assert session.control_socket_path.startswith("tcp://127.0.0.1:")
        endpoint_file = (
            long_workspace / ".run_control" / "run-long.endpoint.json"
        )
        assert json.loads(endpoint_file.read_text(encoding="utf-8")) == {
            "endpoint": session.control_socket_path,
            "pausable": False,
        }
    finally:
        if session.control_socket is not None:
            await session.control_socket.stop()


@__import__("pytest").mark.asyncio
async def test_long_workspace_dashboard_delivers_pause_to_backend(
    tmp_path: Path,
) -> None:
    """Exercise the real dashboard -> gateway -> fallback socket control path."""
    from orchestratord.cli.dashboard import DashboardState

    issue_workspace = tmp_path / ("ISSUE-LIVE-" + "x" * 80)
    session = SimpleNamespace(
        control_socket=None,
        control_socket_path=None,
        run_id="run-live",
        workspace=SimpleNamespace(path=issue_workspace),
    )
    (tmp_path / ".orchestratord_issue_registry.json").write_text(
        json.dumps(
            {
                "issue-live": {
                    "issue_identifier": "ISSUE-LIVE",
                    "status": "running",
                    "workspace_path": str(issue_workspace),
                    "run_id": "run-live",
                    "created_at": 100.0,
                    "updated_at": 200.0,
                }
            }
        ),
        encoding="utf-8",
    )
    assert await BackendRunner._start_control_socket(session) is True
    state = DashboardState(tmp_path)
    try:
        for _ in range(100):
            snapshot = state.refresh_snapshot(force=True)
            issue = snapshot["issues"]["issues"][0]
            if issue["chat_control_available"]:
                break
            await asyncio.sleep(0.01)
        else:
            raise AssertionError("dashboard never attached to fallback endpoint")

        for verb in ("pause", "resume", "stop"):
            assert state.chat_gateway.control("run-live", verb) is True
            command = await asyncio.wait_for(
                session.control_socket._command_queue.get(),
                timeout=1.0,
            )
            assert command.cmd == verb
    finally:
        state.tailer_manager.stop_all()
        state.chat_gateway.stop()
        await session.control_socket.stop()


@__import__("pytest").mark.parametrize("run_kind", ["issue", "agent_followup"])
@__import__("pytest").mark.asyncio
async def test_initial_and_followup_runs_share_lifecycle_controls(
    tmp_path: Path,
    run_kind: str,
) -> None:
    """Run origin must not change pause, resume, or stop availability."""
    run_id = f"run-{run_kind}"
    session = SimpleNamespace(
        control_socket=None,
        control_socket_path=None,
        run_id=run_id,
        run_kind=run_kind,
        workspace=SimpleNamespace(path=tmp_path / run_kind),
    )
    gateway = ChatGateway()
    assert await BackendRunner._start_control_socket(session) is True
    try:
        gateway.sync_active_run_ids({run_id: session.control_socket_path})
        for _ in range(100):
            if session.control_socket._clients:
                break
            await asyncio.sleep(0.01)
        else:
            raise AssertionError("dashboard did not attach to run control endpoint")

        for verb in ("pause", "resume", "stop"):
            assert gateway.control(run_id, verb) is True
            command = await asyncio.wait_for(
                session.control_socket._command_queue.get(), timeout=1.0
            )
            assert command.cmd == verb
    finally:
        gateway.stop()
        await session.control_socket.stop()


class TestChatGatewayLifecycle(IsolatedAsyncioTestCase):
    """Tests for ChatGateway lifecycle management."""

    def test_gateway_creation_and_stop(self):
        gw = ChatGateway()
        self.assertIsNotNone(gw)
        gw.stop()

    def test_sync_active_run_ids_empty(self):
        gw = ChatGateway()
        try:
            gw.sync_active_run_ids({})
        finally:
            gw.stop()

    def test_subscribe_unknown_run(self):
        gw = ChatGateway()
        try:
            q = gw.subscribe("nonexistent")
            self.assertIsNone(q)
        finally:
            gw.stop()

    async def test_unreachable_endpoint_reports_retryable_unavailable(self):
        gw = ChatGateway()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                endpoint = str(Path(tmp) / "not-listening.sock")
                gw.sync_active_run_ids({"run-starting": endpoint})
                first_connection = gw._connections["run-starting"]
                subscription = gw.subscribe("run-starting")
                self.assertIsNotNone(subscription)
                assert subscription is not None
                frame = await asyncio.to_thread(subscription.get, True, 1)
                self.assertEqual(frame["type"], "RunUnavailable")
                self.assertEqual(frame["data"]["reason"], "connect_failed")

                late_subscription = gw.subscribe("run-starting")
                self.assertIsNotNone(late_subscription)
                assert late_subscription is not None
                late_frame = late_subscription.get_nowait()
                self.assertEqual(late_frame["type"], "RunUnavailable")

                gw.sync_active_run_ids({"run-starting": endpoint})
                self.assertIsNot(
                    gw._connections["run-starting"], first_connection
                )
        finally:
            gw.stop()

    def test_send_message_unknown_run(self):
        gw = ChatGateway()
        try:
            ok = gw.send_message("nonexistent", "hello")
            self.assertFalse(ok)
        finally:
            gw.stop()

    def test_control_unknown_run(self):
        gw = ChatGateway()
        try:
            ok = gw.control("nonexistent", "pause")
            self.assertFalse(ok)
        finally:
            gw.stop()

    def test_read_history_no_transcript(self):
        gw = ChatGateway()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                history = gw.read_history("nonexistent")
                self.assertEqual(history, [])
        finally:
            gw.stop()

    async def test_tcp_endpoint_streams_events_and_controls(self):
        """The dashboard bridge works with the Windows loopback transport."""
        control = ControlSocket(tcp=True)
        await control.start()
        gateway = ChatGateway()
        try:
            gateway.sync_active_run_ids({"run-tcp": control.endpoint})
            for _ in range(30):
                if control._clients:
                    break
                await asyncio.sleep(0.02)
            else:
                self.fail("gateway did not connect to the TCP control endpoint")
            queue = gateway.subscribe("run-tcp")
            self.assertIsNotNone(queue)

            await control.send_event({"type": "TextDelta", "data": {"content": "hello"}})
            assert queue is not None
            frame = await asyncio.to_thread(queue.get, True, 1)
            self.assertEqual(frame["type"], "TextDelta")

            self.assertTrue(gateway.send_message("run-tcp", "continue"))
            command = await asyncio.wait_for(
                control._command_queue.get(), timeout=1
            )
            self.assertEqual((command.cmd, command.payload), ("followup", "continue"))

            for verb, payload in (
                ("pause", ""),
                ("resume", "continue after review"),
                ("stop", ""),
            ):
                self.assertTrue(gateway.control("run-tcp", verb, payload))
                command = await asyncio.wait_for(
                    control._command_queue.get(), timeout=1
                )
                self.assertEqual((command.cmd, command.payload), (verb, payload))
        finally:
            gateway.stop()
            await control.stop()


class TestReadToolResult:
    """``event_tailer.read_tool_result`` — the lazy GET completion channel.

    Per ``DESIGN_chat_gateway.md`` §3.3: SSE frames truncate ToolResult
    payloads; this function reads the full content from transcript.jsonl.
    """

    @staticmethod
    def _write_transcript(tmp: str, run_id: str, lines: list[str]) -> Path:
        from orchestratord.paths import SESSIONS_DIR as _unused  # noqa: F401
        import orchestratord.event_tailer as event_tailer_mod

        session_dir = Path(tmp) / run_id
        session_dir.mkdir(parents=True, exist_ok=True)
        transcript = session_dir / "transcript.jsonl"
        transcript.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return transcript

    def test_returns_full_result_and_tool_name(self, tmp_path, monkeypatch):
        import orchestratord.event_tailer as event_tailer_mod

        run_id = "run-1"
        call_id = "call-77"
        lines = [
            json.dumps({
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "id": call_id, "name": "Read",
                     "input": {"path": "src/main.py"}}
                ],
                "ts": "t1",
            }),
            json.dumps({
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": call_id,
                     "is_error": False,
                     "content": [{"type": "text", "text": "LINE1\nLINE2"}]}
                ],
                "ts": "t2",
            }),
        ]
        self._write_transcript(str(tmp_path), run_id, lines)
        monkeypatch.setattr(event_tailer_mod, "SESSIONS_DIR", tmp_path)

        result = event_tailer_mod.read_tool_result(run_id, call_id)
        assert result is not None
        assert result["tool_name"] == "Read"
        assert result["is_error"] is False
        assert result["full_content"] == [{"type": "text", "text": "LINE1\nLINE2"}]
        assert "LINE1\nLINE2" in result["content"]

    def test_unknown_call_id_returns_none(self, tmp_path, monkeypatch):
        import orchestratord.event_tailer as event_tailer_mod

        run_id = "run-1"
        lines = [
            json.dumps({
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "id": "call-1", "name": "Read",
                     "input": {"path": "a.py"}}
                ],
            }),
            json.dumps({
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "call-1",
                     "is_error": False, "content": "ok"}
                ],
            }),
        ]
        self._write_transcript(str(tmp_path), run_id, lines)
        monkeypatch.setattr(event_tailer_mod, "SESSIONS_DIR", tmp_path)

        assert event_tailer_mod.read_tool_result(run_id, "call-missing") is None

    def test_missing_transcript_returns_none(self, tmp_path, monkeypatch):
        import orchestratord.event_tailer as event_tailer_mod

        monkeypatch.setattr(event_tailer_mod, "SESSIONS_DIR", tmp_path)
        assert event_tailer_mod.read_tool_result("no-such-run", "call-1") is None
