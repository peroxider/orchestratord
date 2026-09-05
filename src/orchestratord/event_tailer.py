"""EventTailerManager — 管理 per-run_id 的文件 tailer 线程。

为每个活跃的 agent session（通过 IssueRegistry 中的 run_id 识别）启动
一个后台 tailer 线程，tail 两个文件：
  1. events.ndjson — 每个 tool 调用事件（含 params）
  2. transcript.jsonl — assistant 消息的 tool_use/text 块 + user 消息的 tool_result 块

解析后的事件推入线程安全队列，由 DashboardState.refresh_snapshot() 消费。

文件 tail 采用 byte-offset 轮询（seek/readline/tell），与 Visualizer 的
SessionLiveTail 模式一致。本方案接受此模式的重复（代码库中已有 4 份独立
实现），不提取共享类，以保持 Dashboard 自包含。

设计约束：
- 只用 stdlib（threading, queue, json, pathlib, time, logging）
- 不导入可视化模块（零耦合）
- 兼容 ThreadingHTTPServer 的同步线程模型
"""

from __future__ import annotations

import json
import logging
import queue
import threading
import time
from pathlib import Path
from typing import Any

from .paths import SESSIONS_DIR
from .transcript_compat import normalize_legacy_history

logger = logging.getLogger(__name__)

_MAX_QUEUE_SIZE = 1000
_TAIL_POLL_INTERVAL_S = 0.5
_MAX_RESULT_CONTENT_CHARS = 500
_CODEX_TOOL_ITEM_TYPES = {"command_execution", "file_change", "mcp_tool_call", "web_search"}


def _flatten_content(raw: Any) -> str:
    """Flatten transcript content blocks into displayable text."""
    if raw is None:
        return ""
    if isinstance(raw, str):
        return raw
    if isinstance(raw, list):
        parts: list[str] = []
        for item in raw:
            if isinstance(item, dict):
                parts.append(str(item.get("text", item.get("content", ""))))
            else:
                parts.append(str(item))
        return "\n".join(parts)
    return json.dumps(raw, ensure_ascii=False)


def _truncate_content(raw: Any, max_chars: int = _MAX_RESULT_CONTENT_CHARS) -> str:
    """Flatten transcript content and return a bounded display string."""
    text = _flatten_content(raw)
    if len(text) > max_chars:
        return text[:max_chars] + "…"
    return text


def _history_text(content: Any) -> str | None:
    """Return text when a history payload contains text blocks only."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return None
    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict) or block.get("type") != "text":
            return None
        parts.append(str(block.get("text", "")))
    return "".join(parts)


def _append_history_message(
    messages: list[dict[str, Any]], message: dict[str, Any]
) -> None:
    """Coalesce transport fragments that belong to one assistant message."""
    if messages and message.get("type") == "RunEnded" and messages[-1].get("type") == "SessionComplete":
        # The runner's authoritative terminal record supersedes the backend's
        # completion notification in the conversation, not in raw evidence.
        messages[-1] = message
        return
    if messages and message.get("role") == "assistant" and message.get("ts") not in (None, ""):
        previous = messages[-1]
        if (
            previous.get("role") == "assistant"
            and previous.get("ts") == message.get("ts")
            and previous.get("type") == message.get("type")
        ):
            previous_text = _history_text(previous.get("content"))
            message_text = _history_text(message.get("content"))
            if previous_text is not None and message_text is not None:
                previous["content"] = [
                    {"type": "text", "text": previous_text + message_text}
                ]
                return
    messages.append(message)


def _coalesce_agent_text_events(
    events: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Collapse same-message text fragments before assigning SSE cursors."""
    coalesced: list[dict[str, Any]] = []
    text_positions: dict[tuple[Any, ...], int] = {}
    for event in events:
        if event.get("event_type") != "agent_text" or event.get("source_ts") in (None, ""):
            coalesced.append(event)
            continue
        data = event.get("data") or {}
        key = (
            event.get("run_id"),
            event.get("issue_id"),
            event.get("source_ts"),
            data.get("turn"),
        )
        position = text_positions.get(key)
        if position is None:
            text_positions[key] = len(coalesced)
            coalesced.append(event)
            continue

        previous = coalesced[position]
        previous_data = previous.setdefault("data", {})
        combined = str(previous_data.get("content") or "") + str(data.get("content") or "")
        previous_count = int(previous_data.get("content_char_count") or 0)
        current_count = int(data.get("content_char_count") or 0)
        total_count = previous_count + current_count
        previous_data["content"] = _truncate_content(combined, _MAX_RESULT_CONTENT_CHARS)
        previous_data["content_char_count"] = total_count
        previous_data["content_truncated"] = bool(
            previous_data.get("content_truncated")
            or data.get("content_truncated")
            or total_count > _MAX_RESULT_CONTENT_CHARS
        )
    return coalesced


def read_history_direct(run_id: str) -> list[dict[str, Any]]:
    """Read transcript history for a run without a queue or thread.

    Returns a list of message dicts suitable for the chat gateway's
    SSE ``history`` frame.  Each dict has keys: ``role``, ``content``,
    ``ts`` (timestamp from the transcript line).

    Best-effort: returns an empty list if the transcript file is
    missing or unreadable.
    """
    transcript_path = SESSIONS_DIR / run_id / "transcript.jsonl"
    if not transcript_path.exists():
        # Read-only compatibility for sessions written by older releases.
        transcript_path = (
            Path.home() / ".cache" / "orchestratord" / "sessions" / run_id / "transcript.jsonl"
        )
    if not transcript_path.exists():
        return []

    messages: list[dict[str, Any]] = []
    try:
        with open(transcript_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                # Two formats coexist:
                # 1. Classic transcript: {"role": "...", "content": "...", "ts": "..."}
                # 2. Live frames:       {"type": "TextDelta|ToolCallEvent|...", "data": {...}}
                if "role" in entry:
                    if entry.get("type") in {"SessionComplete", "RunEnded", "Error"}:
                        _append_history_message(messages, _frame_to_history_entry(entry))
                        continue
                    message = {
                        "role": entry.get("role", "unknown"),
                        "content": entry.get("content", ""),
                        "ts": entry.get("ts", ""),
                    }
                    for key in ("type", "origin", "system_prompt", "delivery", "backend", "resume_session_id"):
                        if key in entry:
                            message[key] = entry[key]
                    _append_history_message(messages, message)
                elif "type" in entry:
                    _append_history_message(messages, _frame_to_history_entry(entry))
    except (FileNotFoundError, OSError):
        return []

    return normalize_legacy_history(messages)


def _frame_to_history_entry(frame: dict[str, Any]) -> dict[str, Any]:
    """Convert a live SSE frame to a history entry for the chat UI."""
    frame_type = frame.get("type", "")
    data = frame.get("data", {}) or {}
    ts = frame.get("ts", "")

    if frame_type == "TextDelta":
        return {
            "role": "assistant", "type": "TextDelta",
            "content": data.get("content", ""), "ts": ts,
        }
    if frame_type == "ToolCallEvent":
        return {
            "role": "assistant",
            "content": [{
                "type": "tool_use", "name": data.get("tool_name", "?"),
                "id": data.get("tool_use_id", ""), "input": data.get("params", {}),
            }],
            "ts": ts,
        }
    if frame_type == "ToolResultEvent":
        output = ""
        result = data.get("result", {}) or {}
        if isinstance(result, dict):
            output = result.get("output", "") or str(result)[:500]
        else:
            output = str(result)[:500]
        return {
            "role": "tool",
            "content": [{
                "type": "tool_result", "tool_use_id": data.get("tool_use_id", ""),
                "content": output,
                "is_error": bool(data.get("is_error") or (
                    isinstance(result, dict) and result.get("is_error")
                )),
                "exit_code": data.get("exit_code", (
                    result.get("exit_code") if isinstance(result, dict) else None
                )),
            }],
            "ts": ts,
        }
    if frame_type == "TurnComplete":
        return {"role": "system", "content": f"Turn {data.get('turn', '?')} complete", "ts": ts}
    if frame_type in {"SessionComplete", "RunEnded"}:
        return {"role": "system", "type": frame_type,
                "content": f"Run ended: {data.get('reason', '?')}", "ts": ts,
                "reason": data.get("reason"), "status": data.get("status")}
    if frame_type == "Error":
        return {"role": "system", "type": "Error", "content": data.get("message", "Backend error"), "ts": ts}
    # Unknown frame type — skip.
    return {"role": "system", "content": "", "ts": ts}


def read_tool_result(run_id: str, call_id: str) -> dict[str, Any] | None:
    """Read the full, untruncated tool result for *call_id* from disk.

    The live SSE frames truncate ToolResult payloads (~4KB) to keep
    control-socket frames small; this is the lazy GET completion channel
    for those frames (``DESIGN_chat_gateway.md`` §3.3).  The full content
    lives in ``transcript.jsonl`` as a ``tool_result`` block on the user
    message that follows the corresponding ``tool_use``.

    Returns ``None`` when the transcript is missing or *call_id* cannot
    be found (dashboard answers 404 in that case).
    """
    transcript_path = SESSIONS_DIR / run_id / "transcript.jsonl"
    if not transcript_path.exists():
        # Read-only compatibility for sessions written by older releases.
        transcript_path = (
            Path.home() / ".cache" / "orchestratord" / "sessions" / run_id / "transcript.jsonl"
        )
    if not transcript_path.exists():
        return None

    tool_name: str | None = None
    found: dict[str, Any] | None = None
    try:
        with open(transcript_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                content = entry.get("content")
                if not isinstance(content, list):
                    continue
                role = entry.get("role")
                if role == "assistant":
                    # Register tool_use_id → tool_name for result lookups.
                    for block in content:
                        if not isinstance(block, dict):
                            continue
                        if block.get("type") == "tool_use" and block.get("id") == call_id:
                            tool_name = block.get("name")
                elif role == "user":
                    for block in content:
                        if not isinstance(block, dict):
                            continue
                        if block.get("type") != "tool_result":
                            continue
                        if block.get("tool_use_id") != call_id:
                            continue
                        found = {
                            "tool_name": tool_name or call_id,
                            "is_error": bool(block.get("is_error")),
                            "content": _truncate_content(block.get("content"), _MAX_RESULT_CONTENT_CHARS),
                            "full_content": block.get("content"),
                            "ts": entry.get("ts", ""),
                        }
                        break
                if found is not None:
                    break
    except (FileNotFoundError, OSError):
        return None

    if found is None:
        return None
    # The SSE completion channel serves the full payload; keep the
    # truncated view too so the UI can render either.
    return found


class EventTailerManager:
    """管理 per-run_id 的文件 tailer 线程，产出事件到线程安全队列。"""

    def __init__(self, workspace: Path) -> None:
        self._workspace = workspace
        self._tailers: dict[str, _SessionTailer] = {}
        self._event_queue: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=_MAX_QUEUE_SIZE)
        self._lock = threading.Lock()

    def sync_active_run_ids(self, run_id_to_issue_info: dict[str, tuple[str, Path]]) -> None:
        """启动新 run_id 的 tailer，停止已消失的。

        Args:
            run_id_to_issue_info: 当前活跃的 run_id → (issue_id, workspace_path) 映射。
                来源：IssueRegistry 中 status 为活跃状态且有 run_id 的记录。
        """
        with self._lock:
            # 停止不再活跃的 tailer
            for run_id in list(self._tailers):
                if run_id not in run_id_to_issue_info:
                    self._tailers[run_id].stop()
                    del self._tailers[run_id]
            # 启动新 tailer
            for run_id, (issue_id, issue_workspace) in run_id_to_issue_info.items():
                if run_id and run_id not in self._tailers:
                    tailer = _SessionTailer(
                        run_id=run_id,
                        issue_id=issue_id,
                        workspace=issue_workspace,
                        event_queue=self._event_queue,
                    )
                    self._tailers[run_id] = tailer
                    tailer.start()

    def drain_events(self) -> list[dict[str, Any]]:
        """非阻塞消费队列中的所有待处理事件。"""
        events: list[dict[str, Any]] = []
        while True:
            try:
                events.append(self._event_queue.get_nowait())
            except queue.Empty:
                break
        return _coalesce_agent_text_events(events)

    def load_historical(self, run_id: str, issue_id: str, workspace: Path) -> None:
        """One-shot read of all historical events for a completed session.

        Creates a temporary ``_SessionTailer`` (no thread), calls its read
        methods once to drain ``events.ndjson`` and ``transcript.jsonl`` from
        offset 0, pushing events into the shared queue.  Does NOT start a
        persistent tailer — completed sessions won't produce new events.
        """
        tailer = _SessionTailer(
            run_id=run_id,
            issue_id=issue_id,
            workspace=workspace,
            event_queue=self._event_queue,
        )
        tailer._tail_events_ndjson()
        tailer._tail_transcript()

    def stop_all(self) -> None:
        """停止所有 tailer（atexit 调用）。"""
        with self._lock:
            for tailer in self._tailers.values():
                tailer.stop()
            self._tailers.clear()


class _SessionTailer:
    """单个 session 的文件 tailer，运行在独立线程中。"""

    def __init__(
        self,
        run_id: str,
        issue_id: str,
        workspace: Path,
        event_queue: queue.Queue[dict[str, Any]],
    ) -> None:
        self._run_id = run_id
        self._issue_id = issue_id
        self._workspace = workspace
        self._event_queue = event_queue
        self._running = False
        self._thread: threading.Thread | None = None

        # 文件路径（两级 fallback）
        self._events_ndjson_path = workspace / ".reports" / f"{run_id}.events.ndjson"
        self._events_ndjson_fallback = (
            Path.home() / ".cache" / "orchestratord" / "tool-events" / run_id / "events.ndjson"
        )
        self._transcript_path = SESSIONS_DIR / run_id / "transcript.jsonl"
        self._transcript_fallback = (
            Path.home() / ".cache" / "orchestratord" / "sessions" / run_id / "transcript.jsonl"
        )

        # Byte offsets
        self._events_offset = 0
        self._transcript_offset = 0
        # True once events.ndjson yields any data — prevents double-counting
        # tool_call events from transcript.jsonl's tool_use blocks.
        self._events_ndjson_seen = False
        # tool_use_id → tool_name mapping, so tool_result events can show
        # the human-readable tool name instead of the opaque ID.
        self._tool_name_map: dict[str, str] = {}
        # Codex ``stream-json`` records may be split across multiple
        # transcript text blocks.  Reassemble them before exposing events so
        # the dashboard counts semantic messages and tool activity rather
        # than transport chunks.
        self._codex_wire_buffer = ""
        self._codex_wire_ts: Any = None
        self._codex_started_tools: set[str] = set()

    def start(self) -> None:
        self._running = True
        self._thread = threading.Thread(
            target=self._tail_loop,
            name=f"tailer-{self._run_id[:12]}",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._running = False

    def _tail_loop(self) -> None:
        while self._running:
            try:
                self._tail_events_ndjson()
                self._tail_transcript()
            except Exception as exc:  # noqa: BLE001 - tailers must survive malformed rows
                logger.debug("Tailer %s error: %s", self._run_id, exc)
            time.sleep(_TAIL_POLL_INTERVAL_S)

    def _tail_events_ndjson(self) -> None:
        """Tail events.ndjson — 每个 tool 调用事件（含 params、approved 等）。"""
        path = self._resolve_events_path()
        if path is None:
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                f.seek(self._events_offset)
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    self._events_ndjson_seen = True
                    self._emit_event(
                        "tool_call",
                        {
                            "tool": entry.get("tool", "?"),
                            "tool_use_id": entry.get("tool_use_id"),
                            "approved": entry.get("approved"),
                            "turn": entry.get("turn", 0),
                            "deny_reason": entry.get("deny_reason"),
                            "params": entry.get("params"),
                            "ts": entry.get("ts", ""),
                        },
                    )
                self._events_offset = f.tell()
        except FileNotFoundError:
            pass

    def _tail_transcript(self) -> None:
        """Tail transcript.jsonl — extract tool events.

        Parses two kinds of message lines:
        1. Assistant messages (``role == "assistant"``) — ``tool_use`` content blocks → tool_call events
        2. User messages (``role == "user"``) — ``tool_result`` content blocks → tool_result events

        This makes the dashboard work even when ``audit_log=minimal`` (events.ndjson only
        records denied calls) because transcript.jsonl is always written per-turn.
        """
        transcript_path = (
            self._transcript_path
            if self._transcript_path.exists()
            else self._transcript_fallback
        )
        if not transcript_path.exists():
            return
        try:
            with open(transcript_path, "r", encoding="utf-8") as f:
                f.seek(self._transcript_offset)
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    role = entry.get("role")

                    if role == "assistant":
                        self._process_assistant_message(entry)
                    elif role == "user":
                        self._process_user_message(entry)
                    elif role == "system":
                        self._process_system_message(entry)

                    # Transcript v2 deliberately keeps provider-specific and
                    # unknown events visible.  They are audit observations,
                    # not reasons to fail the rest of the stream.
                    if entry.get("schema_version") == 2 and (
                        entry.get("kind") == "unknown" or entry.get("raw")
                    ):
                        self._emit_event(
                            "raw_event",
                            {
                                "kind": entry.get("kind", "unknown"),
                                "raw": entry.get("raw", {}),
                                "content": entry.get("thinking") or entry.get("text") or "",
                                "stage_id": entry.get("stage_id"),
                                "branch_id": entry.get("branch_id"),
                                "conversation_id": entry.get("conversation_id"),
                                "ts": entry.get("timestamp") or entry.get("ts"),
                            },
                        )

                self._transcript_offset = f.tell()
        except FileNotFoundError:
            pass

    def _process_assistant_message(self, entry: dict[str, Any]) -> None:
        """Extract tool_use + text events from an assistant message.

        If events.ndjson already provided tool_call events (richer data with
        approved/deny_reason), skip tool_use blocks here to avoid double-counting.
        Always emit text blocks (events.ndjson doesn't contain agent text).
        Always register tool_use_id → tool_name for tool_result lookups.
        """
        content = entry.get("content")
        if isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    continue
                block_type = block.get("type")
                if block_type == "tool_use":
                    tool_name = block.get("name", "?")
                    tool_use_id = block.get("id")
                    if tool_use_id:
                        self._tool_name_map[tool_use_id] = tool_name
                    # Only emit tool_call from transcript if events.ndjson
                    # hasn't already provided it (audit_log=minimal/none case)
                    if not self._events_ndjson_seen:
                        self._emit_event(
                            "tool_call",
                            {
                                "tool": tool_name,
                                "approved": True,
                                "turn": block.get("turn", entry.get("turn", 0)),
                                "deny_reason": None,
                                "tool_use_id": tool_use_id,
                                "params": block.get("input"),
                                "ts": entry.get("timestamp") or entry.get("ts"),
                            },
                        )
                elif block_type == "text":
                    text = block.get("text", "")
                    if text and text.strip():
                        source_ts = entry.get("timestamp") or entry.get("ts")
                        if not entry.get("type") and self._consume_codex_wire_text(
                            str(text), source_ts
                        ):
                            continue
                        flattened = _flatten_content(text)
                        self._emit_event(
                            "agent_text",
                            {
                                "content": _truncate_content(flattened, 500),
                                "content_truncated": len(flattened) > 500,
                                "content_char_count": len(flattened),
                                "turn": entry.get("turn", 0),
                                "timestamp_quality": entry.get("timestamp_quality"),
                                "ts": source_ts,
                            },
                        )

    def _consume_codex_wire_text(self, text: str, source_ts: Any) -> bool:
        """Consume a complete or chunked Codex ``stream-json`` record."""
        if not self._codex_wire_buffer:
            stripped = text.lstrip()
            try:
                candidate = json.loads(stripped)
            except json.JSONDecodeError:
                candidate = None
            if isinstance(candidate, dict) and self._is_codex_wire_event(candidate):
                self._process_codex_wire_event(candidate, source_ts)
                return True
            if not stripped.startswith('{"type"'):
                return False
            self._codex_wire_ts = source_ts

        self._codex_wire_buffer += text
        while "\n" in self._codex_wire_buffer:
            line, self._codex_wire_buffer = self._codex_wire_buffer.split("\n", 1)
            self._process_codex_wire_line(line, self._codex_wire_ts)
            self._codex_wire_ts = source_ts if self._codex_wire_buffer else None
        return True

    @staticmethod
    def _is_codex_wire_event(candidate: dict[str, Any]) -> bool:
        event_type = candidate.get("type")
        return isinstance(event_type, str) and (
            event_type.startswith(("thread.", "turn.", "item.")) or event_type == "error"
        )

    def _process_codex_wire_line(self, line: str, source_ts: Any) -> None:
        value = line.strip()
        if not value:
            return
        try:
            event = json.loads(value)
        except json.JSONDecodeError:
            self._emit_agent_text(value, source_ts)
            return
        if self._is_codex_wire_event(event):
            self._process_codex_wire_event(event, source_ts)
        else:
            self._emit_agent_text(value, source_ts)

    def _process_codex_wire_event(self, event: dict[str, Any], source_ts: Any) -> None:
        event_type = event.get("type")
        if event_type == "turn.completed":
            metrics = {
                key: event[key]
                for key in ("usage", "duration_ms", "total_cost_usd")
                if key in event
            }
            if metrics:
                metrics["ts"] = source_ts
                self._emit_event("run_metrics", metrics)
            return
        item = event.get("item") or {}
        if not isinstance(item, dict):
            return
        item_type = item.get("type")
        if event_type == "item.completed" and item_type == "agent_message":
            self._emit_agent_text(str(item.get("text") or ""), source_ts)
            return
        if item_type not in _CODEX_TOOL_ITEM_TYPES:
            return

        tool_use_id = str(item.get("id") or "")
        if not tool_use_id:
            return
        tool_name, params = self._codex_tool_identity(item)
        self._tool_name_map[tool_use_id] = tool_name
        if event_type == "item.started":
            if self._events_ndjson_seen:
                self._codex_started_tools.add(tool_use_id)
            else:
                self._emit_codex_tool_call(tool_use_id, tool_name, params, source_ts)
            return
        if event_type != "item.completed":
            return
        if tool_use_id not in self._codex_started_tools and not self._events_ndjson_seen:
            self._emit_codex_tool_call(tool_use_id, tool_name, params, source_ts)

        output = item.get("aggregated_output")
        if output in (None, ""):
            output = item.get("result") or item.get("status") or "completed"
        flattened = _flatten_content(output)
        exit_code = item.get("exit_code")
        is_error = item.get("status") == "failed" or (
            isinstance(exit_code, int) and not isinstance(exit_code, bool) and exit_code != 0
        )
        self._emit_event(
            "tool_result",
            {
                "tool": tool_name,
                "is_error": is_error,
                "tool_use_id": tool_use_id,
                "result_content": _truncate_content(flattened, 500),
                "content_truncated": len(flattened) > 500,
                "content_char_count": len(flattened),
                "ts": source_ts,
            },
        )

    @staticmethod
    def _codex_tool_identity(item: dict[str, Any]) -> tuple[str, Any]:
        item_type = item.get("type")
        if item_type == "command_execution":
            return "Command", {"command": item.get("command") or ""}
        if item_type == "file_change":
            return "File change", item.get("changes") or []
        if item_type == "web_search":
            return "Web search", {"query": item.get("query") or ""}
        return str(item.get("name") or "MCP tool"), item.get("arguments") or {}

    def _emit_codex_tool_call(
        self,
        tool_use_id: str,
        tool_name: str,
        params: Any,
        source_ts: Any,
    ) -> None:
        self._codex_started_tools.add(tool_use_id)
        self._emit_event(
            "tool_call",
            {
                "tool": tool_name,
                "approved": True,
                "turn": 0,
                "deny_reason": None,
                "tool_use_id": tool_use_id,
                "params": params,
                "ts": source_ts,
            },
        )

    def _emit_agent_text(self, text: str, source_ts: Any) -> None:
        if not text.strip():
            return
        self._emit_event(
            "agent_text",
            {
                "content": _truncate_content(text, 500),
                "content_truncated": len(text) > 500,
                "content_char_count": len(text),
                "ts": source_ts,
            },
        )

    def _process_user_message(self, entry: dict[str, Any]) -> None:
        """Extract tool_result events from a user message's content blocks."""
        content = entry.get("content")
        text = _history_text(content)
        if text:
            self._emit_event(
                "run_input" if entry.get("type") == "RunInput" else "operator_input",
                {"content": text, "origin": entry.get("origin", "operator"),
                 "system_prompt": entry.get("system_prompt", ""),
                 "delivery": entry.get("delivery", "recorded"),
                 "ts": entry.get("timestamp") or entry.get("ts")},
            )
        if isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "tool_result":
                    is_error = bool(block.get("is_error"))
                    tool_use_id = block.get("tool_use_id")
                    # Look up the tool name from the corresponding tool_use block
                    tool_name = self._tool_name_map.get(tool_use_id or "", tool_use_id or "?")
                    raw_content = block.get("content")
                    # Truncate content to avoid queue bloat (keep first 500 chars)
                    flattened = _flatten_content(raw_content)
                    result_content = _truncate_content(flattened, 500)
                    self._emit_event(
                        "tool_result",
                        {
                            "tool": tool_name,
                            "is_error": is_error,
                            "tool_use_id": tool_use_id,
                            "result_content": result_content,
                            "content_truncated": len(flattened) > 500,
                            "content_char_count": len(flattened),
                            "turn": block.get("turn", entry.get("turn", 0)),
                            "timestamp_quality": entry.get("timestamp_quality"),
                            "ts": entry.get("timestamp") or entry.get("ts"),
                        },
                    )

    def _process_system_message(self, entry: dict[str, Any]) -> None:
        """Surface terminal telemetry persisted by the backend runner."""
        frame_type = entry.get("type")
        if frame_type not in {"SessionComplete", "RunEnded", "Error"}:
            return
        data = entry.get("data")
        if not isinstance(data, dict):
            return
        if frame_type in {"RunEnded", "Error"}:
            self._emit_event("run_ended" if frame_type == "RunEnded" else "run_error", {
                **data, "ts": entry.get("timestamp") or entry.get("ts"),
                "content": data.get("summary") or data.get("message") or data.get("reason", ""),
            })
            return
        metrics = {
            key: data[key]
            for key in ("usage", "duration_ms", "total_cost_usd", "reason", "turn")
            if key in data
        }
        if not any(key in metrics for key in ("usage", "duration_ms", "total_cost_usd")):
            return
        metrics["ts"] = entry.get("timestamp") or entry.get("ts")
        self._emit_event("run_metrics", metrics)

    def _emit_event(self, event_type: str, data: dict[str, Any]) -> None:
        """Push a normalized event dict onto the queue."""
        try:
            event = {
                "type": "event",
                "event_type": event_type,
                "issue_id": self._issue_id,
                "run_id": self._run_id,
                "data": data,
                # Ingestion time remains the stable SSE ordering clock.
                "ts": time.time(),
            }
            source_ts = data.get("ts")
            if source_ts not in (None, ""):
                event["source_ts"] = source_ts
            self._event_queue.put_nowait(event)
        except queue.Full:
            pass  # Queue full — skip this event, offset still advances

    def _resolve_events_path(self) -> Path | None:
        """返回存在的 events.ndjson 路径（两级 fallback）。"""
        if self._events_ndjson_path.exists():
            return self._events_ndjson_path
        if self._events_ndjson_fallback.exists():
            return self._events_ndjson_fallback
        return None
