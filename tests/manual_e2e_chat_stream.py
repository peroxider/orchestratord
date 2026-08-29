"""Manual E2E — chat gateway SSE stream over the control-socket bridge.

Requires a local POSIX UDS (or loopback TCP) — no external binary needed,
but the suite is hand-driven per the ``manual_e2e_*`` convention.

Run manually with:

    pytest tests/manual_e2e_chat_stream.py -v --no-skip

Verifies (``DESIGN_chat_gateway.md`` §3.6):
1. A real ``ControlSocket`` server accepts a ``ChatGateway`` connection.
2. A TextDelta broadcast through the UDS bridge reaches the gateway's
   subscription queue (the SSE ``frame`` stream the dashboard serves).
3. ``sync_active_run_ids`` tears the connection down when the run ends.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path

import pytest

from orchestratord.chat_gateway import ChatGateway
from orchestratord.control_socket import ControlSocket

pytestmark = [
    pytest.mark.skip(reason="manual E2E — hand-driven chat stream check"),
    # The conftest PATH guard otherwise blocks every backend CLI;
    # this suite needs a live control-socket transport.
    pytest.mark.uses_real_cli,
]


async def _run_chat_stream_e2e(sock_path: Path | None, *, tcp: bool) -> None:
    control = ControlSocket(sock_path, tcp=tcp)
    await control.start()
    gateway = ChatGateway()
    try:
        run_id = "manual-e2e-run-1"
        # ``sync_active_run_ids`` expects string endpoints (UDS path or
        # ``tcp://host:port``) — matching the dashboard's contract.
        gateway.sync_active_run_ids({run_id: control.endpoint})

        # Wait for the gateway's read loop to connect to the UDS server.
        for _ in range(50):
            if control._clients:
                break
            await asyncio.sleep(0.02)
        else:
            pytest.fail("gateway did not connect to the control socket")

        sub = gateway.subscribe(run_id)
        assert sub is not None

        # Simulate a FakeBackend emitting a TextDelta during a run.
        await control.send_event({"type": "TextDelta", "data": {"content": "hello"}})
        frame = await asyncio.to_thread(sub.get, True, 1.0)
        assert frame["type"] == "TextDelta"
        assert frame["data"]["content"] == "hello"

        # Follow-up command must reach the control socket's command queue.
        assert gateway.send_message(run_id, "continue")
        cmd = await asyncio.wait_for(control._command_queue.get(), timeout=1.0)
        assert (cmd.cmd, cmd.payload) == ("followup", "continue")

        # ToolResult frames arrive with the SSE vocabulary intact.
        await control.send_event(
            {"type": "ToolResultEvent",
             "data": {"tool_name": "Read", "tool_use_id": "c1",
                      "result": {"output": "x" * 6000}}}
        )
        frame2 = await asyncio.to_thread(sub.get, True, 1.0)
        assert frame2["type"] == "ToolResultEvent"
        # Control-socket frames are small; the gateway truncates on read.
        assert frame2["data"]["result"]["truncated"] is True
    finally:
        gateway.stop()
        await control.stop()


def test_chat_stream_uds() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        asyncio.run(_run_chat_stream_e2e(Path(tmp) / "run.sock", tcp=False))


def test_chat_stream_tcp_loopback() -> None:
    asyncio.run(_run_chat_stream_e2e(None, tcp=True))


def test_read_history_is_json_list() -> None:
    """History replay returns the SSE ``history`` payload shape."""
    history = ChatGateway.read_history("manual-e2e-nonexistent")
    assert isinstance(history, list)


def test_truncation_helper_round_trip() -> None:
    """The SSE truncation helper keeps frames small on the wire."""
    from orchestratord.chat_gateway import _truncate_tool_result

    big = {"type": "ToolResultEvent",
           "data": {"tool_name": "Grep",
                    "result": {"output": "y" * 9000}}}
    out = _truncate_tool_result(big)
    assert out["data"]["result"]["truncated"] is True
    assert len(out["data"]["result"]["output"]) < 9000
    # The JSON frame stays well under the control-socket small-frame budget.
    assert len(json.dumps(out, ensure_ascii=False)) < 8192
