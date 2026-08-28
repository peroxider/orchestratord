"""Tests for backend-agnostic followup injection (Phase B)."""

from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path
from unittest import IsolatedAsyncioTestCase

from orchestratord.control_socket import ControlCommand, ControlSocket
from orchestratord.session_state import AgentSession


class TestFollowupInjection(IsolatedAsyncioTestCase):
    """Tests for the followup command in _drain_control_commands."""

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.sock_path = Path(self.tmp.name) / "test.sock"
        self.cs = ControlSocket(self.sock_path)
        await self.cs.start()

    async def asyncTearDown(self):
        await self.cs.stop()
        self.tmp.cleanup()

    async def _send_cmd(self, verb: str, payload: str = "") -> None:
        reader, writer = await asyncio.open_unix_connection(
            str(self.sock_path)
        )
        writer.write(
            (json.dumps({"cmd": verb, "payload": payload}) + "\n").encode("utf-8")
        )
        await writer.drain()
        writer.close()

    async def _drain_one(self) -> ControlCommand:
        async for cmd in self.cs.poll_commands():
            return cmd
        raise TimeoutError("no command received")

    def test_session_has_pending_followups_field(self):
        """AgentSession has _pending_followups field (default factory)."""
        from orchestratord.issue import Issue
        from orchestratord.workspace import Workspace

        issue = Issue(id="test-1", identifier="test-1", title="test")
        ws = Workspace(path=Path("/tmp"), issue_identifier="test-1")
        session = AgentSession(issue=issue, workspace=ws)
        self.assertEqual(session._pending_followups, [])
        session._pending_followups.append("test message")
        self.assertEqual(session._pending_followups, ["test message"])

    async def test_control_cmd_followup_accepted(self):
        """ControlCmd literal includes 'followup'."""
        cmd = ControlCommand(cmd="followup", payload="hello")
        self.assertEqual(cmd.cmd, "followup")
        self.assertEqual(cmd.payload, "hello")

    async def test_followup_command_parsed_by_socket(self):
        """Socket receives followup command correctly."""
        await self._send_cmd("followup", "try plan B")
        cmd = await self._drain_one()
        self.assertEqual(cmd.cmd, "followup")
        self.assertEqual(cmd.payload, "try plan B")

    async def test_followup_pending_queue_cleared(self):
        """_pending_followups can be cleared after use."""
        session = AgentSession(
            issue=type("I", (), {"id": "x", "identifier": "x", "title": "x"})(),
            workspace=type("W", (), {"path": Path("/tmp")})(),
        )
        session._pending_followups.append("msg 1")
        session._pending_followups.append("msg 2")
        self.assertEqual(len(session._pending_followups), 2)
        session._pending_followups.clear()
        self.assertEqual(session._pending_followups, [])