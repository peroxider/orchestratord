"""Scripted fake GitHub Copilot CLI — prints a representative JSONL stream.

Spawned by ``tests/test_copilot_session.py`` via ``sys.executable`` (wired
through ``SessionSpec.runtime_bin``) so the suite never touches the real
``copilot`` binary. Output mirrors the wire format documented in the
multica Go reference (``server/pkg/agent/copilot.go``):

    copilot -p "<prompt>" --output-format json ...
    → NDJSON on stdout: {"type": "...", "data": {...}} lines, ending with a
      synthetic {"type": "result", "sessionId", "exitCode"} line.

The prompt arrives as the value after ``-p`` on the command line — the
fake asserts that channel by echoing it into the streamed text.

Mode (env ``FAKE_COPILOT_MODE``, default ``happy``):

* ``happy``     — deltas + authoritative assistant.message + a tool-only
  turn (toolRequests / tool.execution_complete) + ``result`` (exit 0).
* ``nonstream`` — assistant.message with no prior deltas (exercises the
  TEXT defense path for CLIs that filter deltas).
* ``error``     — ``session.error`` + ``result`` (exitCode 1), exits 1.
* ``hang``      — prints nothing, sleeps forever (total-timeout path).
"""

from __future__ import annotations

import json
import os
import sys
import time
from typing import Any


def _emit(event: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(event) + "\n")
    sys.stdout.flush()


def _prompt_from_argv(argv: list[str]) -> str:
    if "-p" in argv:
        idx = argv.index("-p")
        if idx + 1 < len(argv):
            return argv[idx + 1]
    return ""


def main() -> None:
    mode = os.environ.get("FAKE_COPILOT_MODE", "happy")
    prompt = _prompt_from_argv(sys.argv[1:])

    if mode == "hang":
        time.sleep(300)
        return

    if mode == "error":
        _emit({
            "type": "session.start",
            "data": {"sessionId": "copilot-fake-err", "selectedModel": "gpt-5"},
        })
        _emit({
            "type": "session.error",
            "data": {"errorType": "auth", "message": "authentication required"},
        })
        _emit({
            "type": "result",
            "sessionId": "copilot-fake-err",
            "exitCode": 1,
        })
        sys.exit(1)

    if mode == "nonstream":
        _emit({
            "type": "session.start",
            "data": {"sessionId": "copilot-fake-ns", "selectedModel": "gpt-5"},
        })
        _emit({
            "type": "assistant.message",
            "data": {
                "messageId": "m1",
                "model": "gpt-5",
                "content": "plain answer",
                "toolRequests": [],
            },
        })
        _emit({
            "type": "result",
            "sessionId": "copilot-fake-ns",
            "exitCode": 0,
        })
        return

    # happy — one streaming turn followed by a tool-only turn.
    _emit({
        "type": "session.start",
        "data": {"sessionId": "copilot-fake-123", "selectedModel": "gpt-5"},
    })
    _emit({
        "type": "assistant.message_delta",
        "data": {"messageId": "m1", "deltaContent": f"PROMPT:{prompt} "},
    })
    _emit({
        "type": "assistant.message_delta",
        "data": {"messageId": "m1", "deltaContent": "final answer"},
    })
    _emit({
        "type": "assistant.message",
        "data": {
            "messageId": "m1",
            "model": "gpt-5",
            "content": f"PROMPT:{prompt} final answer",
            "toolRequests": [],
        },
    })
    _emit({
        "type": "assistant.message",
        "data": {
            "messageId": "m2",
            "model": "gpt-5",
            "content": "",
            "toolRequests": [
                {
                    "toolCallId": "tc-1",
                    "name": "shell",
                    "arguments": {"command": "ls"},
                    "type": "tool_call",
                }
            ],
        },
    })
    _emit({
        "type": "tool.execution_complete",
        "data": {
            "toolCallId": "tc-1",
            "success": True,
            "result": {"content": "file-a\nfile-b"},
        },
    })
    _emit({
        "type": "result",
        "sessionId": "copilot-fake-123",
        "exitCode": 0,
    })


if __name__ == "__main__":
    main()
