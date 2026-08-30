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

logger = logging.getLogger(__name__)

_MAX_QUEUE_SIZE = 1000
_TAIL_POLL_INTERVAL_S = 0.5
_MAX_RESULT_CONTENT_CHARS = 500


def _truncate_content(raw: Any, max_chars: int = _MAX_RESULT_CONTENT_CHARS) -> str:
    """Flatten tool_result content to a truncated string.

    Content can be a string, a list of ``{type:"text", text:"..."}`` blocks,
    or other JSON.  Returns at most ``max_chars`` characters.
    """
    if raw is None:
        return ""
    if isinstance(raw, str):
        text = raw
    elif isinstance(raw, list):
        parts: list[str] = []
        for item in raw:
            if isinstance(item, dict):
                parts.append(str(item.get("text", item.get("content", ""))))
            else:
                parts.append(str(item))
        text = "\n".join(parts)
    else:
        text = json.dumps(raw, ensure_ascii=False)
    if len(text) > max_chars:
        return text[:max_chars] + "…"
    return text


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
                    messages.append(
                        {
                            "role": entry.get("role", "unknown"),
                            "content": entry.get("content", ""),
                            "ts": entry.get("ts", ""),
                        }
                    )
                elif "type" in entry:
                    messages.append(_frame_to_history_entry(entry))
    except (FileNotFoundError, OSError):
        return []

    return messages


def _frame_to_history_entry(frame: dict[str, Any]) -> dict[str, Any]:
    """Convert a live SSE frame to a history entry for the chat UI."""
    frame_type = frame.get("type", "")
    data = frame.get("data", {}) or {}
    ts = frame.get("ts", "")

    if frame_type == "TextDelta":
        return {"role": "assistant", "content": data.get("content", ""), "ts": ts}
    if frame_type == "ToolCallEvent":
        return {
            "role": "assistant",
            "content": [{"type": "tool_use", "name": data.get("tool_name", "?"), "id": data.get("tool_use_id", "")}],
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
            "content": [{"type": "tool_result", "tool_use_id": data.get("tool_use_id", ""), "content": output}],
            "ts": ts,
        }
    if frame_type == "TurnComplete":
        return {"role": "system", "content": f"Turn {data.get('turn', '?')} complete", "ts": ts}
    if frame_type == "SessionComplete":
        return {"role": "system", "content": f"Session ended: {data.get('reason', '?')}", "ts": ts}
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
        return events

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
        self._transcript_path = (
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
            except Exception as exc:
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
        if not self._transcript_path.exists():
            return
        try:
            with open(self._transcript_path, "r", encoding="utf-8") as f:
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
                                "turn": 0,
                                "deny_reason": None,
                                "tool_use_id": tool_use_id,
                                "params": block.get("input"),
                            },
                        )
                elif block_type == "text":
                    text = block.get("text", "")
                    if text and text.strip():
                        self._emit_event(
                            "agent_text",
                            {"content": _truncate_content(text, 500)},
                        )

    def _process_user_message(self, entry: dict[str, Any]) -> None:
        """Extract tool_result events from a user message's content blocks."""
        content = entry.get("content")
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
                    result_content = _truncate_content(raw_content, 500)
                    self._emit_event(
                        "tool_result",
                        {
                            "tool": tool_name,
                            "is_error": is_error,
                            "tool_use_id": tool_use_id,
                            "result_content": result_content,
                        },
                    )

    def _emit_event(self, event_type: str, data: dict[str, Any]) -> None:
        """Push a normalized event dict onto the queue."""
        try:
            self._event_queue.put_nowait(
                {
                    "type": "event",
                    "event_type": event_type,
                    "issue_id": self._issue_id,
                    "run_id": self._run_id,
                    "data": data,
                    "ts": time.time(),
                }
            )
        except queue.Full:
            pass  # Queue full — skip this event, offset still advances

    def _resolve_events_path(self) -> Path | None:
        """返回存在的 events.ndjson 路径（两级 fallback）。"""
        if self._events_ndjson_path.exists():
            return self._events_ndjson_path
        if self._events_ndjson_fallback.exists():
            return self._events_ndjson_fallback
        return None
