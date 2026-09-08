"""Scripted fake Reasonix ACP agent — a minimal JSON-RPC 2.0 stdio server for tests.

Spawned by ``tests/test_reasonix_session.py`` through an executable ``sh``
shim (that execs ``sys.executable`` on this file), so the real ``reasonix``
binary is never touched.  It speaks the ACP wire subset ``ReasonixSession``'s
client side drives (ported from multica ``server/pkg/agent/reasonix.go``):

* ``initialize``            → ``{protocolVersion: 1}``
* ``session/new``           → ``{sessionId: "fake-reasonix-1"}``
* ``session/resume``        → ``{sessionId: "fake-reasonix-resumed"}``
* ``session/set_model``     → ``{}`` (or an error frame in ``setmodel-error``)
* ``session/prompt``        → streams ``session/update`` notifications then
  resolves with a ``stopReason`` (or an error / nothing / exits).
* ``session/request_permission`` (agent→client) — waits for the client's
  ``RequestPermissionOutcome`` before continuing, echoing ``answered:
  <optionId>`` as the next chunk.

The shim must launch this as ``reasonix acp …`` — argv[1] must be ``acp``
and the fixed sandbox flags must be present, mirroring
``reasonixACPLaunchArgs``.

Mode (last argv element, selected by the test shim): ``immediate`` |
``approval`` | ``protected`` | ``question`` | ``cancel`` |
``provider-error`` | ``prompt-error`` | ``exit`` | ``hang`` | ``resume`` |
``model`` | ``setmodel-error`` | ``echo`` | ``stop-error`` |
``stop-weird`` | ``noresult``.
"""

from __future__ import annotations

import json
import sys
from typing import Any


def _emit(message: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()


def _result(msg_id: Any, result: dict[str, Any]) -> None:
    _emit({"jsonrpc": "2.0", "id": msg_id, "result": result})


def _error(msg_id: Any, message: str) -> None:
    _emit({"jsonrpc": "2.0", "id": msg_id, "error": {"code": -32000, "message": message}})


def _update(session_id: str, update: dict[str, Any]) -> None:
    _emit(
        {
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {"sessionId": session_id, "update": update},
        }
    )


def _chunk(session_id: str, text: str) -> None:
    _update(
        session_id,
        {
            "sessionUpdate": "agent_message_chunk",
            "content": {"type": "text", "text": text},
        },
    )


def _deferred_tool_start(session_id: str) -> None:
    _update(
        session_id,
        {
            "sessionUpdate": "tool_call",
            "toolCallId": "tc-1",
            "title": "Run command: ls",
        },
    )


def _deferred_tool_args(session_id: str, cumulative: str) -> None:
    _update(
        session_id,
        {
            "sessionUpdate": "tool_call_update",
            "toolCallId": "tc-1",
            "status": "pending",
            "content": [{"type": "text", "text": cumulative}],
        },
    )


def _tool_completed(
    session_id: str,
    call_id: str = "tc-1",
    output: Any = None,
) -> None:
    update: dict[str, Any] = {
        "sessionUpdate": "tool_call_update",
        "toolCallId": call_id,
        "status": "completed",
    }
    if output is not None:
        update["rawOutput"] = output
    _update(session_id, update)


def _raw_input_tool(session_id: str) -> None:
    _update(
        session_id,
        {
            "sessionUpdate": "tool_call",
            "toolCallId": "tc-2",
            "title": "Read file: /tmp/notes.md",
            "rawInput": {"path": "/tmp/notes.md"},
        },
    )


def _request_permission(session_id: str, mode: str) -> None:
    if mode == "question":
        # Reasonix user question: ask-* call id + a ":cancel" option.
        tool_call: dict[str, Any] = {
            "toolCallId": "ask-1",
            "title": "Which database should I use?",
        }
        options = [
            {"optionId": "a:cancel", "kind": "cancel"},
            {"optionId": "deny", "kind": "reject_once"},
        ]
    elif mode == "protected":
        # Fresh-human-only decision via trusted metadata.
        tool_call = {
            "toolCallId": "tc-9",
            "title": "Remember preference",
            "_meta": {"reasonix.io": {"tool": "remember", "fresh": False}},
        }
        options = [
            {"optionId": "allow", "kind": "allow_once"},
            {"optionId": "deny", "kind": "reject_once"},
        ]
    else:  # approval — an ordinary auto-approvable tool permission
        tool_call = {
            "toolCallId": "tc-3",
            "title": "Run command: rm build/",
        }
        options = [
            {"optionId": "allow", "kind": "allow_once"},
            {"optionId": "deny", "kind": "reject_once"},
        ]
    _emit(
        {
            "jsonrpc": "2.0",
            "id": 9001,
            "method": "session/request_permission",
            "params": {
                "sessionId": session_id,
                "toolCall": tool_call,
                "options": options,
            },
        }
    )


def _immediate_stream(session_id: str) -> None:
    _chunk(session_id, "Hello ")
    _chunk(session_id, "world")
    _deferred_tool_start(session_id)
    _deferred_tool_args(session_id, '{"command":')
    _deferred_tool_args(session_id, '{"command": "ls -la"}')
    _tool_completed(session_id, output={"output": "total 0"})
    _raw_input_tool(session_id)
    _tool_completed(session_id, call_id="tc-2", output="file contents")


def main() -> None:
    if len(sys.argv) < 3 or sys.argv[1] != "acp" or "--workspace-only" not in sys.argv:
        sys.stderr.write("fake reasonix: must be launched as `reasonix acp …`\n")
        sys.exit(2)
    mode = sys.argv[-1]
    session_id = "fake-reasonix-1"
    pending_prompt_id: Any = None
    permission_id: Any = None

    if mode == "provider-error":
        sys.stderr.write("❌ upstream HTTP 502 Non-retryable\n")
        sys.stderr.flush()

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
            if mode == "resume":
                _error(rid, "session not found")
            else:
                _result(rid, {"sessionId": session_id})
        elif method == "session/resume":
            if mode == "resume":
                _result(rid, {"sessionId": "fake-reasonix-resumed"})
            else:
                _result(rid, {"sessionId": session_id})
        elif method == "session/set_model":
            if mode == "setmodel-error":
                _error(rid, "no such model")
            else:
                _result(rid, {})
        elif method == "session/prompt":
            pending_prompt_id = rid
            if mode == "prompt-error":
                _error(rid, "reasonix exploded")
                pending_prompt_id = None
            elif mode == "exit":
                sys.exit(0)
            elif mode == "hang":
                pass  # never answer; the client turn timeout fires
            elif mode == "model":
                _chunk(session_id, "model:deepseek-r2")
                _result(rid, {"stopReason": "end_turn"})
                pending_prompt_id = None
            elif mode == "echo":
                prompt_text = msg["params"]["prompt"][0]["text"]
                _chunk(session_id, prompt_text)
                _result(rid, {"stopReason": "end_turn"})
                pending_prompt_id = None
            elif mode == "resume":
                _chunk(session_id, "resumed-ok")
                _result(rid, {"stopReason": "end_turn"})
                pending_prompt_id = None
            elif mode == "stop-error":
                _chunk(session_id, "partial answer")
                _result(rid, {"stopReason": "error"})
                pending_prompt_id = None
            elif mode == "stop-weird":
                _result(rid, {"stopReason": "max_tokens"})
                pending_prompt_id = None
            elif mode == "noresult":
                _result(rid, {})
                pending_prompt_id = None
            elif mode == "cancel":
                _chunk(session_id, "hello ")
                # wait for session/cancel before answering
            elif mode in ("approval", "protected", "question"):
                _chunk(session_id, "Hello ")
                permission_id = 9001
                _request_permission(session_id, mode)
                # wait for the client's outcome before continuing
            else:  # immediate / provider-error / setmodel-error
                _immediate_stream(session_id)
                _result(rid, {"stopReason": "end_turn"})
                pending_prompt_id = None
        elif method == "session/cancel":
            if mode == "cancel" and pending_prompt_id is not None:
                _chunk(session_id, "cancel-received")
                _result(pending_prompt_id, {"stopReason": "cancelled"})
                pending_prompt_id = None
            else:
                _chunk(session_id, "cancel-received")
        elif rid is not None and rid == permission_id:
            # The client's RequestPermissionOutcome response.
            outcome = (msg.get("result") or {}).get("outcome") or {}
            _chunk(session_id, f"answered:{outcome.get('optionId')}")
            if pending_prompt_id is not None:
                _result(pending_prompt_id, {"stopReason": "end_turn"})
                pending_prompt_id = None
            permission_id = None


if __name__ == "__main__":
    main()
