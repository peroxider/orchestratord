"""Scripted fake Cursor Agent CLI — prints a representative stream-json stream.

Spawned by ``tests/test_cursor_session.py`` via ``sys.executable`` (wired
through ``SessionSpec.runtime_bin``) so the suite never touches the real
``cursor-agent`` binary. Output mirrors the wire format documented in the
multica Go reference (``server/pkg/agent/cursor.go``):

    cursor-agent -p --output-format stream-json --yolo ...
    → NDJSON on stdout: {"type": "system" | "assistant" | "tool_call" |
      "thinking" | "result" | ...} lines; the prompt arrives on stdin
      (read to EOF).

The fake asserts the stdin channel by echoing the prompt into the first
assistant text block, and one line carries the ``stdout:`` prefix the
real CLI sometimes emits (the session must strip it).

Mode (env ``FAKE_CURSOR_MODE``, default ``happy``):

* ``happy`` — init, streamed text, thinking, a nested ``tool_call`` pair,
  a legacy flat ``tool_use``/``tool_result`` pair, a final assistant text
  block, then a ``result`` (success) event.
* ``error`` — ``system`` ``subtype:"error"``, then exits 1 without a
  ``result`` event.
* ``hang``  — prints nothing, sleeps forever (total-timeout path).
"""

from __future__ import annotations

import json
import os
import sys
import time
from typing import Any


def _emit(event: dict[str, Any], *, prefix: str = "") -> None:
    sys.stdout.write(prefix + json.dumps(event) + "\n")
    sys.stdout.flush()


def main() -> None:
    mode = os.environ.get("FAKE_CURSOR_MODE", "happy")
    prompt = sys.stdin.read()

    if mode == "hang":
        time.sleep(300)
        return

    if mode == "error":
        _emit({
            "type": "system",
            "subtype": "error",
            "session_id": "fake-cursor-err",
            "error": "provider unavailable",
        })
        sys.exit(1)

    # happy
    _emit({
        "type": "system",
        "subtype": "init",
        "session_id": "fake-cursor-1",
    })
    _emit(
        {
            "type": "assistant",
            "session_id": "fake-cursor-1",
            "message": {
                "model": "composer-1",
                "content": [{"type": "text", "text": f"PROMPT:{prompt} "}],
            },
        },
        # The real CLI sometimes prefixes stream lines with "stdout:".
        prefix="stdout: ",
    )
    _emit({
        "type": "thinking",
        "subtype": "delta",
        "session_id": "fake-cursor-1",
        "text": " pondering",
    })
    _emit({
        "type": "thinking",
        "subtype": "completed",
        "session_id": "fake-cursor-1",
    })
    _emit({
        "type": "tool_call",
        "subtype": "started",
        "session_id": "fake-cursor-1",
        # The real CLI packs two ids into one newline-separated string.
        "call_id": "call-1\nfc_9",
        "tool_call": {
            "readToolCall": {"args": {"path": "a.txt"}},
            "toolCallId": "call-1",
        },
    })
    _emit({
        "type": "tool_call",
        "subtype": "completed",
        "session_id": "fake-cursor-1",
        "call_id": "call-1",
        "tool_call": {
            "readToolCall": {"args": {"path": "a.txt"}, "result": {"body": "file body"}},
            "toolCallId": "call-1",
        },
    })
    _emit({
        "type": "tool_use",
        "session_id": "fake-cursor-1",
        "tool_name": "edit_file",
        "tool_id": "toolu-2",
        "parameters": {"path": "b.txt"},
    })
    _emit({
        "type": "tool_result",
        "session_id": "fake-cursor-1",
        "tool_id": "toolu-2",
        "output": "ok",
    })
    _emit({
        "type": "assistant",
        "session_id": "fake-cursor-1",
        "message": {
            "model": "composer-1",
            "content": [{"type": "output_text", "text": "All done"}],
        },
    })
    _emit({
        "type": "result",
        "subtype": "success",
        "session_id": "fake-cursor-1",
        # Repeats the transcript: must NOT be re-emitted as TEXT because
        # assistant blocks already populated the output.
        "result": f"PROMPT:{prompt} All done",
        "is_error": False,
    })


if __name__ == "__main__":
    main()
