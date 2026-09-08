"""Scripted fake ``zeroclaw acp`` CLI for the zeroclaw backend tests.

Reproduces the frames a real ZeroClaw 0.8.4 binary was observed to send
over its ACP JSON-RPC 2.0 stdio transport (ported from the Go suite's
``fakeZeroclawACPScript`` in multica ``agent/zeroclaw_test.go``):

  - initialize carries ``_meta.zeroclaw.defaultModel`` and advertises
    ``sessionCapabilities.resume`` (unless ``ZEROCLAW_NO_RESUME_CAP``).
  - session/new returns ``{sessionId, workspaceDir}`` and nothing else;
    with ``ZEROCLAW_REQUIRE_AGENT_ALIAS`` it fails -32602.
  - session/resume answers a bare ``{}``; ``ZEROCLAW_SESSION_NOT_FOUND``
    fails with ZeroClaw's custom -32000; ``ZEROCLAW_STALE_REPLAY`` pushes
    a historical agent_message_chunk before answering (the replay the
    turn gate must swallow).
  - an unknown session is -32000 SESSION_NOT_FOUND, not a standard code.
  - session/prompt streams two agent_message_chunk frames ("CURRENT " +
    "ANSWER"), optionally a tool_call/tool_call_update pair
    (``ZEROCLAW_TOOL_CALL``), optionally a session/request_permission
    round-trip (``ZEROCLAW_PERMISSION`` / ``ZEROCLAW_LEGACY_CHOICE``),
    then answers ``{stopReason, usage}``.
  - ``ZEROCLAW_HANG`` sleeps forever on session/new (timeout tests).

Diagnostics: every stdin line is appended to ``$ZEROCLAW_REQUESTS_FILE``
and the process argv to ``$ZEROCLAW_ARGV_FILE`` when those are set.
"""

from __future__ import annotations

import json
import os
import sys
import time


def _emit(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def _record(line: str) -> None:
    path = os.environ.get("ZEROCLAW_REQUESTS_FILE")
    if path:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    argv_path = os.environ.get("ZEROCLAW_ARGV_FILE")
    if argv_path and not os.path.exists(argv_path):
        with open(argv_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(sys.argv[1:]) + "\n")


def _chunk(text: str) -> dict:
    return {
        "jsonrpc": "2.0",
        "method": "session/update",
        "params": {
            "sessionId": "ses_existing",
            "update": {
                "sessionUpdate": "agent_message_chunk",
                "content": {"type": "text", "text": text},
            },
        },
    }


def main() -> None:
    for raw in sys.stdin:
        line = raw.strip()
        if not line:
            continue
        _record(line)
        msg = json.loads(line)
        msg_id = msg.get("id")
        method = msg.get("method")

        if method == "initialize":
            session_caps = (
                '{"close":{}}'
                if os.environ.get("ZEROCLAW_NO_RESUME_CAP")
                else '{"close":{},"resume":{}}'
            )
            _emit({
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {
                    "protocolVersion": 1,
                    "_meta": {
                        "zeroclaw": {
                            "defaultModel": "llama3.2",
                            "maxSessions": 10,
                            "sessionTimeoutSecs": 3600,
                        }
                    },
                    "agentInfo": {"name": "zeroclaw-acp", "version": "0.8.4"},
                    "authMethods": [],
                    "agentCapabilities": {
                        "loadSession": True,
                        "sessionCapabilities": json.loads(session_caps),
                    },
                },
            })

        elif method == "session/new":
            if os.environ.get("ZEROCLAW_REQUIRE_AGENT_ALIAS"):
                _emit({
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "error": {
                        "code": -32602,
                        "message": "session/new requires `agentAlias` "
                        "(alias of a configured [agents.<alias>] entry)",
                    },
                })
                return
            params = msg.get("params") or {}
            _emit({
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {
                    "sessionId": "ses_zeroclaw_new",
                    "workspaceDir": params.get("cwd") or "/tmp",
                },
            })

        elif method == "session/resume":
            if os.environ.get("ZEROCLAW_SESSION_NOT_FOUND"):
                _emit({
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "error": {
                        "code": -32000,
                        "message": "Session not found: ses_gone",
                    },
                })
                return
            if os.environ.get("ZEROCLAW_STALE_REPLAY"):
                _emit(_chunk("STALE PRIOR ANSWER"))
            _emit({"jsonrpc": "2.0", "id": msg_id, "result": {}})

        elif method == "session/prompt":
            if os.environ.get("ZEROCLAW_TOOL_CALL"):
                _emit({
                    "jsonrpc": "2.0",
                    "method": "session/update",
                    "params": {
                        "sessionId": "ses_existing",
                        "update": {
                            "sessionUpdate": "tool_call",
                            "toolCallId": "call_1",
                            "name": "terminal",
                            "rawInput": {"command": "echo hi"},
                        },
                    },
                })
                _emit({
                    "jsonrpc": "2.0",
                    "method": "session/update",
                    "params": {
                        "sessionId": "ses_existing",
                        "update": {
                            "sessionUpdate": "tool_call_update",
                            "toolCallId": "call_1",
                            "status": "completed",
                            "rawOutput": "hi\n",
                        },
                    },
                })
            _emit(_chunk("CURRENT "))
            _emit(_chunk("ANSWER"))
            if os.environ.get("ZEROCLAW_PERMISSION"):
                _emit({
                    "jsonrpc": "2.0",
                    "id": "zc-perm-0",
                    "method": "session/request_permission",
                    "params": {
                        "sessionId": "ses_existing",
                        "options": [
                            {"optionId": "allow-once", "kind": "allow_once"},
                            {"optionId": "reject-once", "kind": "reject_once"},
                        ],
                    },
                })
                answer_raw = sys.stdin.readline()
                _record(answer_raw)
                answer = json.loads(answer_raw)
                outcome = (answer.get("result") or {}).get("outcome") or {}
                if outcome.get("optionId") != "allow-once":
                    sys.exit(2)
            if os.environ.get("ZEROCLAW_LEGACY_CHOICE"):
                _emit({
                    "jsonrpc": "2.0",
                    "id": "zc-perm-0",
                    "method": "session/request_permission",
                    "params": {
                        "sessionId": "ses_existing",
                        "options": [
                            {"optionId": "choice-0", "kind": "allow_once"},
                            {"optionId": "choice-1", "kind": "allow_once"},
                            {"optionId": "choice-2", "kind": "reject_once"},
                        ],
                    },
                })
                answer_raw = sys.stdin.readline()
                _record(answer_raw)
                answer = json.loads(answer_raw)
                err = answer.get("error") or {}
                if err.get("code") != -32603:
                    sys.exit(2)
                _emit({
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "error": {
                        "code": -32000,
                        "message": "ACP request_permission failed: "
                        "no auto-selectable permission option offered",
                    },
                })
                return
            if os.environ.get("ZEROCLAW_HANG"):
                time.sleep(30)
            _emit({
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {
                    "stopReason": "end_turn",
                    "usage": {
                        "inputTokens": 10,
                        "outputTokens": 20,
                        "cacheReadTokens": 3,
                        "cacheWriteTokens": 2,
                        "costUsdTicks": 900,
                    },
                },
            })

        else:
            _emit({
                "jsonrpc": "2.0",
                "id": msg_id,
                "error": {"code": -32601, "message": "Method not found"},
            })


if __name__ == "__main__":
    main()
