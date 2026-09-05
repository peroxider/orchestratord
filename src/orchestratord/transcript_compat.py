"""Read-only migration of legacy native transcripts to common chat messages.

New adapters must emit EventEnvelope events. This compatibility reader is only
for old, untyped assistant records; never apply it to current TextDelta frames.
It has no dependency on an installed backend and never rewrites durable files.
"""

from __future__ import annotations

import json
from typing import Any


def _text(message: dict[str, Any]) -> str | None:
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list) and all(
        isinstance(block, dict) and block.get("type") == "text" for block in content
    ):
        return "".join(str(block.get("text", "")) for block in content)
    return None


def normalize_legacy_history(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Normalize adjacent untyped text records, preserving input order/metadata.

    Requiring multiple recognized records avoids treating an ordinary JSON
    answer as a native transcript. Unknown or malformed lines remain visible.
    Typed records and common tool blocks pass through unchanged.
    """
    result: list[dict[str, Any]] = []
    chunk: list[dict[str, Any]] = []

    def is_legacy_candidate(message: dict[str, Any]) -> bool:
        return (
            message.get("role") == "assistant"
            and not message.get("type")
            and _text(message) is not None
        )

    candidates = [message for message in messages if is_legacy_candidate(message)]
    # Detect once per run, but decode in place: operator inputs can separate
    # a stream header from a later single output record.
    is_wire = bool(candidates) and _decode_legacy_codex(candidates) is not candidates

    def flush() -> None:
        if chunk:
            result.extend(
                _decode_legacy_codex(chunk, minimum_records=1) if is_wire else chunk
            )
            chunk.clear()

    for message in messages:
        if is_legacy_candidate(message):
            chunk.append(message)
        else:
            flush()
            result.append(message)
    flush()
    return result


def _decode_legacy_codex(
    messages: list[dict[str, Any]], *, minimum_records: int = 2
) -> list[dict[str, Any]]:
    wire = "".join(_text(message) or "" for message in messages)
    lines = wire.splitlines(keepends=True)
    events: list[dict[str, Any] | None] = []
    for line in lines:
        try:
            value = json.loads(line)
        except (ValueError, TypeError):
            value = None
        events.append(value if isinstance(value, dict) else None)

    def recognized(event: dict[str, Any] | None) -> bool:
        if not event:
            return False
        kind = event.get("type")
        if not isinstance(kind, str):
            return False
        if kind in {
            "thread.started",
            "turn.started",
            "turn.completed",
            "turn.failed",
            "error",
        }:
            return True
        item = event.get("item")
        return (
            kind in {"item.started", "item.updated", "item.completed"}
            and isinstance(item, dict)
            and isinstance(item.get("type"), str)
            and item.get("type")
            in {
                "agent_message",
                "command_execution",
                "file_change",
                "mcp_tool_call",
                "web_search",
                "todo_list",
            }
        )

    if sum(recognized(event) for event in events) < minimum_records:
        return messages

    result: list[dict[str, Any]] = []
    tools: set[str] = set()
    offset = 0
    message_index = 0
    boundary = len(_text(messages[0]) or "")

    def emit(role: str, content: Any, ts: Any, **metadata: Any) -> None:
        if role == "assistant" and isinstance(content, str):
            metadata.setdefault("type", "TextDelta")
        result.append({"role": role, "content": content, "ts": ts, **metadata})

    for line, event in zip(lines, events):
        while offset >= boundary and message_index < len(messages) - 1:
            message_index += 1
            boundary += len(_text(messages[message_index]) or "")
        ts = messages[message_index].get("ts", "")
        offset += len(line)
        if not recognized(event):
            if line.strip():
                emit("assistant", line, ts)
            continue
        assert event is not None
        kind = event["type"]
        item = event.get("item") or {}
        if not isinstance(item, dict):
            item = {}
        item_type = item.get("type")
        if kind in {"error", "turn.failed"}:
            error = (
                event.get("error")
                or event.get("message")
                or "The agent reported an error."
            )
            if isinstance(error, dict):
                error = error.get("message") or json.dumps(error, ensure_ascii=False)
            emit("system", str(error), ts, type="Error")
        elif kind == "item.completed" and item_type == "agent_message":
            emit("assistant", str(item.get("text") or ""), ts)
        elif kind in {"item.updated", "item.completed"} and item_type == "todo_list":
            todos = item.get("items") or []
            if isinstance(todos, list) and todos:
                completed = sum(
                    bool(todo.get("completed"))
                    for todo in todos
                    if isinstance(todo, dict)
                )
                emit("system", f"Plan progress · {completed}/{len(todos)} complete", ts)
        elif kind in {"item.started", "item.completed"} and item_type in {
            "command_execution",
            "file_change",
            "mcp_tool_call",
            "web_search",
        }:
            call_id = str(item.get("id") or f"legacy-tool-{len(tools)}")
            name, arguments = {
                "command_execution": ("Command", item.get("command", "")),
                "file_change": ("File change", item.get("changes", [])),
                "mcp_tool_call": (
                    item.get("name") or "MCP tool",
                    item.get("arguments", {}),
                ),
                "web_search": ("Web search", item.get("query", "")),
            }[item_type]
            if call_id not in tools:
                emit(
                    "assistant",
                    [
                        {
                            "type": "tool_use",
                            "id": call_id,
                            "name": name,
                            "input": arguments,
                        }
                    ],
                    ts,
                )
                tools.add(call_id)
            if kind == "item.completed":
                output = item.get(
                    "aggregated_output",
                    item.get("result", item.get("status", "completed")),
                )
                exit_code = item.get("exit_code")
                failed = item.get("status") == "failed" or (
                    isinstance(exit_code, int) and exit_code != 0
                )
                emit(
                    "tool",
                    [
                        {
                            "type": "tool_result",
                            "tool_use_id": call_id,
                            "content": output,
                            "is_error": failed,
                            "exit_code": exit_code,
                        }
                    ],
                    ts,
                )
    return result
