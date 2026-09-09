"""Phase 1 tests: socket persistence + event broadcasting.

Covers:
  * ``_event_to_broadcast_dict`` handles PhaseComplete / TurnComplete /
    SessionComplete (new in Phase 1).
  * ``_broadcast_to_socket`` helper sends events to connected clients
    and is a no-op when no socket is attached.

Uses ``unittest.IsolatedAsyncioTestCase`` (the repo's canonical async
test pattern) and ``tempfile.TemporaryDirectory`` for isolation.
"""

from __future__ import annotations

import asyncio
import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from orchestratord.runner_utils import (
    _broadcast_to_socket as runner_broadcast_to_socket,
)
from orchestratord.runner_utils import (
    _event_to_broadcast_dict as runner_event_to_broadcast_dict,
)

# ------------------------------------------------------------------
# _event_to_broadcast_dict — new event types
# ------------------------------------------------------------------


class TestEventToBroadcastDict(unittest.TestCase):
    """Phase 1: PhaseComplete / TurnComplete / SessionComplete branches."""

    def test_phase_complete(self) -> None:
        from orchestratord.events.agent_events import PhaseComplete

        d = runner_event_to_broadcast_dict(PhaseComplete(phase=3, turn_count=3))
        self.assertEqual(d, {"phase": 3, "turn_count": 3})

    def test_turn_complete(self) -> None:
        from orchestratord.events.agent_events import TurnComplete

        d = runner_event_to_broadcast_dict(TurnComplete(turn=5))
        self.assertEqual(d, {"turn": 5})

    def test_session_complete(self) -> None:
        from orchestratord.events.agent_events import SessionComplete

        d = runner_event_to_broadcast_dict(SessionComplete(reason="task_complete"))
        self.assertEqual(d, {"reason": "task_complete"})

    def test_unknown_event_returns_empty(self) -> None:
        d = runner_event_to_broadcast_dict(object())
        self.assertEqual(d, {})


# ------------------------------------------------------------------
# _broadcast_to_socket — async helper
# ------------------------------------------------------------------


class TestBroadcastToSocket(unittest.IsolatedAsyncioTestCase):
    """Phase 1: _broadcast_to_socket sends to connected clients."""

    async def test_broadcast_no_socket_is_noop(self) -> None:
        """Broadcasting with control_socket=None must not raise."""
        from orchestratord.agent_runner import AgentSession
        from orchestratord.issue_registry.issue import Issue
        from orchestratord.workspace import Workspace

        session = AgentSession(
            subject=Issue(id="I", identifier="I", title="t"),
            workspace=Workspace(path="/tmp", issue_identifier="I", issue_id="I"),
        )
        # control_socket is None by default
        from orchestratord.events.agent_events import PhaseComplete

        await runner_broadcast_to_socket(session, PhaseComplete(phase=1, turn_count=1))

    @unittest.skipIf(os.name == "nt", "Unix-domain socket transport is unavailable on Windows")
    async def test_broadcast_sends_to_connected_client(self) -> None:
        """A connected client receives the broadcast frame."""
        from orchestratord.agent_runner import AgentSession
        from orchestratord.control_socket import ControlSocket, send_cmd  # noqa: F401
        from orchestratord.events.agent_events import PhaseComplete
        from orchestratord.issue_registry.issue import Issue
        from orchestratord.workspace import Workspace

        with TemporaryDirectory() as tmp:
            ws_path = Path(tmp) / "ws"
            ws_path.mkdir()
            sock_path = ws_path / ".run_control" / "test.sock"
            cs = ControlSocket(sock_path)
            await cs.start()
            try:
                session = AgentSession(
                    subject=Issue(id="I", identifier="I", title="t"),
                    workspace=Workspace(path=str(ws_path), issue_identifier="I", issue_id="I"),
                    run_id="run-1",
                )
                session.control_socket = cs

                reader, writer = await asyncio.open_unix_connection(str(sock_path))

                # The server registers the client writer only once its
                # accept task has run — ``open_unix_connection`` returning
                # does not guarantee that yet (the connect can complete off
                # the accept queue). A single ``sleep(0)`` tick is a race:
                # a broadcast before registration is silently dropped
                # (``send_event`` no-ops on an empty client set). Poll until
                # the server has registered us, with a bounded wait.
                for _ in range(100):
                    if cs._clients:
                        break
                    await asyncio.sleep(0.01)
                else:
                    self.fail("control socket never registered the client")

                await runner_broadcast_to_socket(
                    session, PhaseComplete(phase=1, turn_count=1)
                )
                raw = await asyncio.wait_for(reader.readline(), timeout=5)
                frame = json.loads(raw.decode("utf-8"))
                self.assertEqual(frame["type"], "PhaseComplete")
                self.assertEqual(frame["data"], {"phase": 1, "turn_count": 1})
            finally:
                writer.close()
                try:
                    await writer.wait_closed()
                except Exception:
                    pass
                await cs.stop()
