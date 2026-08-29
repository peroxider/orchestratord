"""Tests for chat gateway bridge (Phase C)."""

from __future__ import annotations

import asyncio
import json
import tempfile
import time
from pathlib import Path
from unittest import IsolatedAsyncioTestCase

from orchestratord.chat_gateway import ChatGateway, _truncate_tool_result
from orchestratord.control_socket import ControlSocket
from orchestratord.runner_utils import _broadcast_to_socket
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
