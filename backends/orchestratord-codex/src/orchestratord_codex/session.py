"""Codex CLI session with typed, arrival-timed event streaming."""

from __future__ import annotations

import asyncio
import json
import os
import time
from collections.abc import AsyncIterator
from typing import Any

from orchestratord.spi.approval import ApprovalDecision
from orchestratord.spi.backend import SessionSpec
from orchestratord.spi.capabilities import BackendCapabilities
from orchestratord.spi.events import EventEnvelope, EventKind
from orchestratord.spi.session import ResumeStatus

logger = __import__("logging").getLogger(__name__)

_DEFAULT_TOTAL_TIMEOUT_S = 600.0
_END_OF_STREAM = object()
_REASONING_OVERRIDE_ENV = "ORCHESTRATORD_CODEX_REASONING_EFFORT"
_SUPPORTED_REASONING_EFFORTS = {
    "none",
    "minimal",
    "low",
    "medium",
    "high",
    "xhigh",
}
_TOOL_ITEM_TYPES = {
    "command_execution",
    "file_change",
    "mcp_tool_call",
    "web_search",
}


class CodexSession:
    """Adapt ``codex exec --json`` into the backend-neutral event SPI.

    ``send`` starts the subprocess and returns once its reader task is
    scheduled. ``events`` drains a queue as JSONL rows arrive, preserving the
    arrival time of each native event instead of timestamping the whole turn
    after ``communicate`` finishes.
    """

    def __init__(self, spec: SessionSpec) -> None:
        self._spec = spec
        self.session_id = spec.resume_session_id or f"codex-{id(self)}"
        self.capabilities = BackendCapabilities(
            streaming_deltas=False,
            resumable=True,
            interrupt=False,
            approval_hooks=False,
            parallel_sessions=True,
            cost_reporting=False,
            tool_filtering=False,
            takeover=False,
            resume_detection=False,
        )
        self._queue: asyncio.Queue[EventEnvelope | object] = asyncio.Queue()
        self._seq = 0
        self._turn = 0
        self._closed = False
        self._run_task: asyncio.Task[None] | None = None
        self._process: asyncio.subprocess.Process | None = None
        self._started_tools: set[str] = set()
        self._usage: dict[str, Any] = {}
        self._turn_complete_emitted = False

    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq

    def _now(self) -> float:
        return time.time()

    def _envelope(
        self,
        kind: EventKind,
        payload: dict[str, Any],
        *,
        timestamp: float | None = None,
    ) -> EventEnvelope:
        return EventEnvelope(
            seq=self._next_seq(),
            timestamp=self._now() if timestamp is None else timestamp,
            kind=kind,
            payload=payload,
        )

    def _emit(
        self,
        kind: EventKind,
        payload: dict[str, Any],
        *,
        timestamp: float | None = None,
    ) -> None:
        self._queue.put_nowait(self._envelope(kind, payload, timestamp=timestamp))

    async def send(self, content: str | list[Any]) -> None:
        if self._closed:
            raise RuntimeError("session closed")
        if self._run_task is not None and not self._run_task.done():
            raise RuntimeError("codex turn already running")
        if self._run_task is not None:
            raise RuntimeError("codex CLI session has already completed")

        text = content if isinstance(content, str) else str(content)
        self._run_task = asyncio.create_task(self._run_turn(text))
        # Give immediate spawn errors a chance to enter the event queue while
        # keeping send/event consumption truly concurrent.
        await asyncio.sleep(0)

    def _build_argv(self, text: str) -> tuple[list[str], bytes | None]:
        args = ["codex"]
        reasoning_effort = str(
            self._spec.env.get(_REASONING_OVERRIDE_ENV, "")
        ).strip().lower()
        if reasoning_effort:
            if reasoning_effort not in _SUPPORTED_REASONING_EFFORTS:
                supported = ", ".join(sorted(_SUPPORTED_REASONING_EFFORTS))
                raise ValueError(
                    f"{_REASONING_OVERRIDE_ENV} must be one of: {supported}"
                )
            # Global Codex options must precede the ``exec`` subcommand. This
            # provides a workflow-scoped compatibility escape hatch when a
            # newer desktop config contains an effort unsupported by the
            # installed CLI, without rewriting the user's global config.toml.
            args.extend(
                ["-c", f'model_reasoning_effort="{reasoning_effort}"']
            )
        args.extend(["exec", "--json"])
        if self._spec.model:
            args.extend(["-m", self._spec.model])
        if self._spec.resume_session_id:
            args.extend(["resume", self._spec.resume_session_id])
            return args, text.encode("utf-8")
        # Keep the prompt out of argv.  Besides avoiding process-list leaks,
        # stdin prevents a valid Markdown prompt beginning with ``---`` from
        # being parsed as an unknown Codex CLI option.
        args.append("-")
        return args, text.encode("utf-8")

    async def _run_turn(self, text: str) -> None:
        reason = "success"
        stderr_task: asyncio.Task[bytes] | None = None
        try:
            args, stdin_payload = self._build_argv(text)
            child_env = dict(os.environ)
            child_env.update(self._spec.env)
            self._process = await asyncio.create_subprocess_exec(
                *args,
                cwd=self._spec.cwd or None,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=child_env,
            )
            process = self._process
            assert process.stdin is not None
            if stdin_payload is not None:
                process.stdin.write(stdin_payload)
                await process.stdin.drain()
            process.stdin.close()

            assert process.stderr is not None
            stderr_task = asyncio.create_task(process.stderr.read())
            timeout = self._spec.total_timeout_s or _DEFAULT_TOTAL_TIMEOUT_S
            try:
                async with asyncio.timeout(timeout):
                    assert process.stdout is not None
                    while line := await process.stdout.readline():
                        arrived_at = self._now()
                        value = line.decode("utf-8", errors="replace").strip()
                        if not value:
                            continue
                        try:
                            native = json.loads(value)
                        except json.JSONDecodeError:
                            self._emit(
                                EventKind.TEXT,
                                {
                                    "text": value,
                                    "native_type": "non_json_stdout",
                                    "timestamp_quality": "arrival",
                                },
                                timestamp=arrived_at,
                            )
                            continue
                        if isinstance(native, dict):
                            for event in self._translate_wire_event(
                                native, timestamp=arrived_at
                            ):
                                self._queue.put_nowait(event)
                    return_code = await process.wait()
            except TimeoutError:
                reason = "timeout"
                process.kill()
                await process.wait()
                self._emit(
                    EventKind.ERROR,
                    {
                        "code": "codex_timeout",
                        "message": f"codex CLI exceeded {timeout:.1f}s",
                    },
                )
                return_code = -1

            stderr = ""
            if stderr_task is not None:
                stderr = (await stderr_task).decode(
                    "utf-8", errors="replace"
                ).strip()
                stderr_task = None
            if return_code != 0 and reason == "success":
                reason = "error"
                self._emit(
                    EventKind.ERROR,
                    {
                        "code": "codex_exit",
                        "message": (
                            f"codex CLI exited with code {return_code}; "
                            f"stderr={stderr[:500]}"
                        ),
                    },
                )
        except FileNotFoundError as exc:
            reason = "error"
            self._emit(
                EventKind.ERROR,
                {"code": "codex_spawn_error", "message": str(exc)},
            )
        except asyncio.CancelledError:
            reason = "interrupted"
            raise
        except Exception as exc:  # noqa: BLE001 - backend boundary
            reason = "error"
            self._emit(
                EventKind.ERROR,
                {
                    "code": "codex_error",
                    "message": f"{type(exc).__name__}: {exc}",
                },
            )
        finally:
            if stderr_task is not None:
                stderr_task.cancel()
                await asyncio.gather(stderr_task, return_exceptions=True)
            if not self._turn_complete_emitted:
                self._emit(
                    EventKind.TURN_COMPLETE,
                    {
                        "turn": max(1, self._turn),
                        "reason": reason,
                        "usage": dict(self._usage),
                    },
                )
            self._emit(
                EventKind.SESSION_COMPLETE,
                {
                    "reason": reason,
                    "usage": dict(self._usage),
                    "session_id": self.session_id,
                },
            )
            self._queue.put_nowait(_END_OF_STREAM)
            self._process = None

    def _translate_wire_event(
        self, native: dict[str, Any], *, timestamp: float | None = None
    ) -> list[EventEnvelope]:
        """Translate one Codex JSONL row into zero or more typed events."""
        arrived_at = self._now() if timestamp is None else timestamp
        event_type = str(native.get("type") or "")
        if event_type == "thread.started":
            thread_id = native.get("thread_id")
            if thread_id:
                self.session_id = str(thread_id)
            return []
        if event_type == "turn.started":
            self._turn += 1
            self._turn_complete_emitted = False
            return []
        if event_type == "turn.completed":
            usage = native.get("usage")
            if isinstance(usage, dict):
                self._usage = dict(usage)
            self._turn_complete_emitted = True
            payload: dict[str, Any] = {
                "turn": max(1, self._turn),
                "reason": "success",
                "usage": dict(self._usage),
                "native_type": event_type,
                "timestamp_quality": "arrival",
            }
            if "duration_ms" in native:
                payload["duration_ms"] = native["duration_ms"]
            return [
                self._envelope(
                    EventKind.TURN_COMPLETE, payload, timestamp=arrived_at
                )
            ]
        if event_type == "error":
            message = native.get("message", native.get("error", "Codex error"))
            if isinstance(message, dict):
                message = message.get("message") or json.dumps(
                    message, ensure_ascii=False
                )
            return [
                self._envelope(
                    EventKind.ERROR,
                    {
                        "code": str(native.get("code") or "codex_error"),
                        "message": str(message),
                        "native_type": event_type,
                    },
                    timestamp=arrived_at,
                )
            ]

        item = native.get("item")
        if not isinstance(item, dict):
            return [
                self._envelope(
                    EventKind.UNKNOWN,
                    {"event": event_type, "raw": dict(native)},
                    timestamp=arrived_at,
                )
            ]
        item_type = str(item.get("type") or "")
        if event_type == "item.completed" and item_type == "agent_message":
            text = str(item.get("text") or "")
            return (
                [
                    self._envelope(
                        EventKind.TEXT,
                        {
                            "text": text,
                            "turn": max(1, self._turn),
                            "native_type": item_type,
                            "timestamp_quality": "arrival",
                        },
                        timestamp=arrived_at,
                    )
                ]
                if text
                else []
            )
        if item_type not in _TOOL_ITEM_TYPES:
            return [
                self._envelope(
                    EventKind.UNKNOWN,
                    {"event": event_type, "raw": dict(native)},
                    timestamp=arrived_at,
                )
            ]

        call_id = str(item.get("id") or f"codex-tool-{self._seq + 1}")
        name, arguments = self._tool_identity(item)
        output: list[EventEnvelope] = []
        if event_type == "item.started":
            self._started_tools.add(call_id)
            output.append(
                self._tool_call(
                    call_id, name, arguments, item_type, timestamp=arrived_at
                )
            )
            return output
        if event_type != "item.completed":
            return [
                self._envelope(
                    EventKind.UNKNOWN,
                    {"event": event_type, "raw": dict(native)},
                    timestamp=arrived_at,
                )
            ]
        if call_id not in self._started_tools:
            self._started_tools.add(call_id)
            output.append(
                self._tool_call(
                    call_id, name, arguments, item_type, timestamp=arrived_at
                )
            )

        result = item.get("aggregated_output")
        if result in (None, ""):
            result = item.get("result", item.get("status", "completed"))
        exit_code = item.get("exit_code")
        failed = item.get("status") == "failed" or (
            isinstance(exit_code, int)
            and not isinstance(exit_code, bool)
            and exit_code != 0
        )
        output.append(
            self._envelope(
                EventKind.TOOL_RESULT,
                {
                    "call_id": call_id,
                    "name": name,
                    "ok": not failed,
                    "output": result,
                    "exit_code": exit_code,
                    "turn": max(1, self._turn),
                    "native_type": item_type,
                    "timestamp_quality": "arrival",
                },
                timestamp=arrived_at,
            )
        )
        return output

    def _tool_call(
        self,
        call_id: str,
        name: str,
        arguments: Any,
        native_type: str,
        *,
        timestamp: float,
    ) -> EventEnvelope:
        return self._envelope(
            EventKind.TOOL_CALL,
            {
                "call_id": call_id,
                "name": name,
                "arguments": arguments,
                "turn": max(1, self._turn),
                "native_type": native_type,
                "timestamp_quality": "arrival",
            },
            timestamp=timestamp,
        )

    @staticmethod
    def _tool_identity(item: dict[str, Any]) -> tuple[str, Any]:
        item_type = item.get("type")
        if item_type == "command_execution":
            return "Command", {"command": item.get("command") or ""}
        if item_type == "file_change":
            return "File change", item.get("changes") or []
        if item_type == "web_search":
            return "Web search", {"query": item.get("query") or ""}
        return str(item.get("name") or "MCP tool"), item.get("arguments") or {}

    async def _emit_events(self) -> AsyncIterator[EventEnvelope]:
        while True:
            event = await self._queue.get()
            if event is _END_OF_STREAM:
                return
            assert isinstance(event, EventEnvelope)
            yield event

    def events(self) -> AsyncIterator[EventEnvelope]:
        return self._emit_events()

    async def interrupt(self) -> None:
        # The capability remains false because the CLI has no resumable
        # protocol-level interrupt. ``close`` still terminates a child during
        # orchestrator shutdown or stop handling.
        return None

    async def approve(self, request_id: str, decision: ApprovalDecision) -> None:
        return None

    async def probe_resume(self) -> ResumeStatus:
        if not self._spec.resume_session_id:
            return ResumeStatus.RESUMED
        return ResumeStatus.UNDETECTABLE

    async def close(self) -> None:
        self._closed = True
        process = self._process
        if process is not None and process.returncode is None:
            process.terminate()
        task = self._run_task
        if task is not None and not task.done():
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=5.0)
            except TimeoutError:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
