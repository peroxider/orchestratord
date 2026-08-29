"""Tests for chat gateway bridge (Phase C)."""

from __future__ import annotations

import json
import tempfile
import time
from pathlib import Path
from unittest import IsolatedAsyncioTestCase

from orchestratord.chat_gateway import ChatGateway, _truncate_tool_result
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
