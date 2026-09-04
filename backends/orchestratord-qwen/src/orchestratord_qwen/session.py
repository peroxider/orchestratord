"""QwenSession — wraps ``qwen -p --output-format stream-json`` into SPI.

Spawn-per-turn with streaming: stdout is read line-by-line while the
subprocess runs, parsed as NDJSON, and each ``content_block_delta``
frame becomes a ``TEXT_DELTA`` event. This is the only P1 backend in
§8 that genuinely claims ``streaming_deltas=True``.

Mirrors CodexSession's queue + reader-task structure (see
``backends/orchestratord-codex/src/orchestratord_codex/session.py``).
"""

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


class QwenSession:
    """Adapt ``qwen -p --output-format stream-json`` into the AgentSession SPI."""

    def __init__(self, spec: SessionSpec) -> None:
        self._spec = spec
        self.session_id = spec.resume_session_id or f"qwen-{id(self)}"
        self.capabilities = BackendCapabilities(
            streaming_deltas=True,
            resumable=False,
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
        self._turn_complete_emitted = False
        self._closed = False
        self._run_task: asyncio.Task[None] | None = None
        self._process: asyncio.subprocess.Process | None = None

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
        self._queue.put_nowait(
            self._envelope(kind, payload, timestamp=timestamp)
        )

    async def send(self, content: str | list[Any]) -> None:
        if self._closed:
            raise RuntimeError("session closed")
        if self._run_task is not None and not self._run_task.done():
            raise RuntimeError("qwen turn already running")
        if self._run_task is not None:
            raise RuntimeError("qwen CLI session has already completed")

        text = content if isinstance(content, str) else str(content)
        self._run_task = asyncio.create_task(self._run_turn(text))
        await asyncio.sleep(0)

    def _build_argv(self, text: str) -> tuple[list[str], bytes]:
        """Build argv for ``qwen -p --output-format stream-json``.

        We pass the prompt via stdin (mirroring codex CLI): avoids
        process-list leaks and prevents markdown prompts beginning
        with ``---`` from being parsed as unknown options.
        """
        args = ["qwen", "-p", "--output-format", "stream-json"]
        if self._spec.model:
            args.extend(["-m", self._spec.model])
        args.append("-")  # read prompt from stdin
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
                        "code": "qwen_timeout",
                        "message": f"qwen CLI exceeded {timeout:.1f}s",
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
                        "code": "qwen_exit",
                        "message": (
                            f"qwen CLI exited with code {return_code}; "
                            f"stderr={stderr[:500]}"
                        ),
                    },
                )
        except FileNotFoundError as exc:
            reason = "error"
            self._emit(
                EventKind.ERROR,
                {"code": "qwen_spawn_error", "message": str(exc)},
            )
        except asyncio.CancelledError:
            reason = "interrupted"
            raise
        except Exception as exc:  # noqa: BLE001 - backend boundary
            reason = "error"
            self._emit(
                EventKind.ERROR,
                {
                    "code": "qwen_error",
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
                    {"reason": reason},
                )
            self._emit(
                EventKind.SESSION_COMPLETE,
                {
                    "reason": reason,
                    "session_id": self.session_id,
                },
            )
            self._queue.put_nowait(_END_OF_STREAM)
            self._process = None

    def _translate_wire_event(
        self, native: dict[str, Any], *, timestamp: float | None = None
    ) -> list[EventEnvelope]:
        """Translate one Qwen NDJSON row into zero or more typed events.

        Recognized frames (qwen --output-format stream-json convention):

        * ``content_block_delta`` with ``delta.type == "text_delta"``
          → ``TEXT_DELTA`` (this is the primary streaming surface)
        * ``message.start`` / ``content_block_start`` →
          no event (bookkeeping only)
        * ``message.stop`` / ``content_block_stop`` →
          ``TURN_COMPLETE``
        * ``error`` → ``ERROR``
        * anything else → ``UNKNOWN`` with raw payload preserved
        """
        arrived_at = self._now() if timestamp is None else timestamp
        event_type = str(native.get("type") or "")
        if event_type == "content_block_delta":
            delta = native.get("delta")
            if not isinstance(delta, dict):
                return []
            delta_type = str(delta.get("type") or "")
            if delta_type != "text_delta":
                return []
            text = str(delta.get("text") or "")
            if not text:
                return []
            self._turn_complete_emitted = False
            return [
                self._envelope(
                    EventKind.TEXT_DELTA,
                    {
                        "text": text,
                        "delta": text,
                        "native_type": event_type,
                        "timestamp_quality": "arrival",
                    },
                    timestamp=arrived_at,
                )
            ]
        if event_type in {"message.stop", "content_block_stop"}:
            self._turn_complete_emitted = True
            return [
                self._envelope(
                    EventKind.TURN_COMPLETE,
                    {
                        "reason": "success",
                        "native_type": event_type,
                        "timestamp_quality": "arrival",
                    },
                    timestamp=arrived_at,
                )
            ]
        if event_type == "error":
            message = native.get("message", native.get("error", "Qwen error"))
            if isinstance(message, dict):
                message = message.get("message") or json.dumps(
                    message, ensure_ascii=False
                )
            return [
                self._envelope(
                    EventKind.ERROR,
                    {
                        "code": str(native.get("code") or "qwen_error"),
                        "message": str(message),
                        "native_type": event_type,
                    },
                    timestamp=arrived_at,
                )
            ]
        if event_type in {"message.start", "content_block_start"}:
            return []
        return [
            self._envelope(
                EventKind.UNKNOWN,
                {"event": event_type, "raw": dict(native)},
                timestamp=arrived_at,
            )
        ]

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
        return None

    async def approve(self, request_id: str, decision: ApprovalDecision) -> None:
        return None

    async def probe_resume(self) -> ResumeStatus:
        """qwen has no cross-process resume probe."""
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