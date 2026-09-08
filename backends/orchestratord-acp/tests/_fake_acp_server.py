"""Scripted fake ACP agent — a minimal JSON-RPC 2.0 stdio server for tests.

Spawned by ``tests/test_acp.py`` via ``sys.executable`` so it never touches
the real grok / codebuddy / qwenpaw binaries. It speaks just enough of the
ACP wire protocol to exercise ``AcpSession``'s client side:

* ``initialize``        → ``{protocolVersion: 1}``
* ``session/new``       → ``{sessionId: "fake-session-1"}``
* ``session/prompt``    → streams two ``agent_message_chunk`` deltas, a
  ``tool_call`` update, and a ``session/request_permission`` request, then
  resolves with ``stopReason``.
* ``session/cancel``    → streams a ``cancel-received`` delta.

Mode (argv[1]) selects prompt behavior:

* ``immediate`` — emit the full stream then ``stopReason=end_turn`` without
  waiting for the permission response (base event-order test).
* ``approval``  — after ``session/request_permission``, wait for the client's
  ``RequestPermissionOutcome`` response, emit ``permission-granted``, then
  ``end_turn`` (approve round-trip).
* ``cancel``    — emit one delta, wait for ``session/cancel``, emit
  ``cancel-received``, then ``stopReason=cancel`` (interrupt round-trip).
"""

from __future__ import annotations

import json
import sys
from typing import Any


def _emit(message: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()


def _chunk(session_id: str, text: str) -> None:
    _emit(
        {
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {
                "sessionId": session_id,
                "update": {
                    "sessionUpdate": "agent_message_chunk",
                    "content": {"type": "text", "text": text},
                },
            },
        }
    )


def _tool_call(session_id: str) -> None:
    _emit(
        {
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {
                "sessionId": session_id,
                "update": {
                    "sessionUpdate": "tool_call",
                    "toolCallId": "tc-1",
                    "title": "Bash",
                    "args": {"command": "echo hi"},
                },
            },
        }
    )


def _request_permission(session_id: str) -> None:
    _emit(
        {
            "jsonrpc": "2.0",
            "id": 9001,
            "method": "session/request_permission",
            "params": {
                "sessionId": session_id,
                "toolCall": {
                    "toolCallId": "tc-2",
                    "name": "Bash",
                    "args": {"command": "rm -rf /"},
                },
            },
        }
    )


def _result(msg_id: int, result: dict[str, Any]) -> None:
    _emit({"jsonrpc": "2.0", "id": msg_id, "result": result})


def main() -> None:
    mode = sys.argv[1] if len(sys.argv) > 1 else "immediate"
    session_id = "fake-session-1"
    pending_prompt_id: int | None = None
    pending_permission_id: int | None = None

    for raw in sys.stdin:
        raw = raw.strip()
        if not raw:
            continue
        msg = json.loads(raw)
        method = msg.get("method")
        rid = msg.get("id")

        if method == "initialize":
            _result(rid, {"protocolVersion": 1})
        elif method == "session/new":
            _result(rid, {"sessionId": session_id})
        elif method == "session/prompt":
            pending_prompt_id = rid
            if mode == "cancel":
                _chunk(session_id, "hello ")
                # wait for session/cancel (handled in the cancel branch)
            elif mode == "approval":
                _chunk(session_id, "hello ")
                _chunk(session_id, "world")
                _tool_call(session_id)
                pending_permission_id = 9001
                _request_permission(session_id)
                # wait for the permission response (handled below)
            else:  # immediate
                _chunk(session_id, "hello ")
                _chunk(session_id, "world")
                _tool_call(session_id)
                _request_permission(session_id)
                _result(rid, {"stopReason": "end_turn"})
                pending_prompt_id = None
        elif method == "session/cancel":
            if mode == "cancel" and pending_prompt_id is not None:
                _chunk(session_id, "cancel-received")
                _result(pending_prompt_id, {"stopReason": "cancel"})
                pending_prompt_id = None
            else:
                _chunk(session_id, "cancel-received")
        elif rid is not None and rid == pending_permission_id:
            # Client's RequestPermissionOutcome response to our request.
            if mode == "approval" and pending_prompt_id is not None:
                _chunk(session_id, "permission-granted")
                _result(pending_prompt_id, {"stopReason": "end_turn"})
                pending_prompt_id = None
                pending_permission_id = None


if __name__ == "__main__":
    main()
