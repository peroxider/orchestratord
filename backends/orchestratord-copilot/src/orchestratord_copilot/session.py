"""CopilotSession — spawn ``copilot`` and translate its JSONL event stream.

Protocol summary (``copilot -p "<prompt>" --output-format json``)
=================================================================

One-shot spawn-per-turn invocation ported from the multica Go reference
(``server/pkg/agent/copilot.go`` + ``copilot_invocation.go``):

    copilot -p "<prompt>" --output-format json --allow-all --no-ask-user
            [--model <model>] [--resume <session-id>] [custom args]

The prompt is passed as a CLI *argument* — the v1 pipe mode has no stdin
prompt channel (which is exactly why the Go port bypasses the Windows npm
launchers instead of quoting harder).  Events arrive as newline-delimited
JSON on stdout, one object per line:

    { "type": "dotted.event.name", "data": {...}, "id": "...",
      "timestamp": "...", "parentId": "...", "ephemeral": bool }

The final line is a synthetic ``result`` event with top-level fields:

    { "type": "result", "sessionId": "...", "exitCode": 0, "usage": {...} }

Translation table → :class:`EventEnvelope` / :class:`EventKind`:

* ``assistant.message_delta``             → ``TEXT_DELTA`` (genuine incremental
  fragments — this is why the backend claims ``streaming_deltas``)
* ``assistant.message`` content           → ``TEXT`` only when the CLI streamed
  no deltas for the turn.  When deltas already streamed the turn's text,
  re-emitting the authoritative content would duplicate it in the chat
  bridge's TEXT/TEXT_DELTA fold; when the CLI streams nothing, the full
  message is the only copy the transcript gets — the mirror image of the
  Go port buffering deltas as defense-in-depth against a mid-turn death.
* ``assistant.message`` ``reasoningText`` → ``UNKNOWN`` ``{"thinking": ...}``
* ``assistant.message`` ``toolRequests``  → ``TOOL_CALL`` (one per request)
* ``tool.execution_complete``             → ``TOOL_RESULT`` (``ok``/``output``;
  failures rendered as ``"Error: <message>"`` per the Go port)
* ``assistant.reasoning(_delta)``         → ``UNKNOWN`` ``{"thinking": ...}``
* ``session.error``                       → ``ERROR`` + terminal ``reason="error"``
* ``session.warning`` / unrecognized      → ``UNKNOWN`` (auditable, non-fatal)
* ``result``                              → session-id capture; a non-zero
  ``exitCode`` fails the run (appended to any protocol error, mirroring Go
  ``withCopilotExitCode``)
* ``assistant.turn_start`` / usage events → dropped (status/usage only — no
  transcript content in the orchestratord SPI)

Token usage (``assistant.usage`` / ``session.shutdown``) is accumulated into
``Result.Usage`` by the Go reference, but the orchestratord SPI has no usage
channel and ``cost_reporting`` stays False, so it is not translated.

Lifecycle: exit 0 with no ``session.error`` → ``TURN_COMPLETE`` +
``SESSION_COMPLETE`` with ``reason="success"``; ``session.error`` or a
non-zero exit → ``reason="error"`` (stderr tail folded into the ERROR
message); the total timeout kills the child and ends with
``reason="timeout"``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
from collections.abc import AsyncIterator
from typing import Any

from orchestratord.spi.approval import ApprovalDecision
from orchestratord.spi.backend import SessionSpec
from orchestratord.spi.capabilities import BackendCapabilities
from orchestratord.spi.events import EventEnvelope, EventKind
from orchestratord.spi.session import ResumeStatus

logger = logging.getLogger(__name__)

_DEFAULT_TOTAL_TIMEOUT_S = 600.0


class CopilotSession:
    """Adapt the ``copilot`` JSONL stream into the AgentSession SPI."""

    # Flags the daemon hardcodes; user-configured custom args must never
    # override them (multica copilot.go ``copilotBlockedArgs``).
    _BLOCKED_STANDALONE = frozenset({
        "--allow-all",
        "--allow-all-tools",
        "--allow-all-paths",
        "--allow-all-urls",
        "--yolo",
        "--no-ask-user",
        "--acp",
    })
    _BLOCKED_WITH_VALUE = frozenset({"-p", "--output-format", "--resume"})

    def __init__(self, spec: SessionSpec) -> None:
        self._spec = spec
        self.session_id = spec.resume_session_id or f"copilot-{id(self)}"
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
        self._events: list[EventEnvelope] = []
        self._seq = 0
        self._closed = False
        # Per-send translation state (reset at the top of send()).
        self._native_session_id: str | None = None
        self._protocol_error: str | None = None
        self._turn_deltas = False

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq

    def _now(self) -> float:
        return time.time()

    def _envelope(
        self, kind: EventKind, payload: dict[str, Any]
    ) -> EventEnvelope:
        return EventEnvelope(
            seq=self._next_seq(), timestamp=self._now(), kind=kind,
            payload=payload,
        )

    def _binary(self) -> str:
        """Resolve the CLI binary: explicit override → ``copilot``."""
        return (self._spec.runtime_bin or "").strip() or "copilot"

    def _build_argv(self, prompt: str) -> list[str]:
        """Build the argv for one ``copilot -p`` invocation (copilot.go).

        The prompt rides on the command line, exactly like the Go port —
        ``copilot -p`` is a value flag, not a boolean switch.
        """
        binary = self._binary()
        prefix: list[str] = []
        if binary.endswith(".py"):
            # A runtime_bin pointing at a Python script is launched through
            # the running interpreter — how the test suite wires the
            # scripted fake CLI (tests/_fake_copilot_cli.py) in place of
            # the real binary.
            prefix = [sys.executable]
        args = [*prefix, binary, "-p", prompt, "--output-format", "json"]
        # Full headless mode: tools + paths + URLs, no interactive prompts.
        args.extend(["--allow-all", "--no-ask-user"])
        if self._spec.model:
            args.extend(["--model", self._spec.model])
        if self._spec.resume_session_id:
            args.extend(["--resume", self._spec.resume_session_id])
        args.extend(self._filter_custom_args())
        return args

    def _filter_custom_args(self) -> list[str]:
        """Append user-configured custom args minus the daemon's flags.

        Mirrors multica's ``filterCustomArgs`` + ``copilotBlockedArgs``:
        blocked standalone flags are dropped; blocked value flags also
        swallow the argument that follows them.
        """
        custom = self._spec.extra.get("custom_args")
        if not isinstance(custom, (list, tuple)):
            return []
        out: list[str] = []
        skip_next = False
        for raw in custom:
            arg = str(raw)
            if skip_next:
                skip_next = False
                continue
            if arg in self._BLOCKED_WITH_VALUE:
                skip_next = True
                continue
            if arg in self._BLOCKED_STANDALONE:
                continue
            out.append(arg)
        return out

    # ------------------------------------------------------------------
    # AgentSession Protocol
    # ------------------------------------------------------------------

    async def send(self, content: str | list[Any]) -> None:
        if self._closed:
            raise RuntimeError("session closed")

        prompt = content if isinstance(content, str) else str(content)
        argv = self._build_argv(prompt)
        timeout = self._spec.total_timeout_s or _DEFAULT_TOTAL_TIMEOUT_S
        env = {**os.environ, **self._spec.env} if self._spec.env else None

        self._native_session_id = None
        self._protocol_error = None
        self._turn_deltas = False

        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                cwd=self._spec.cwd or None,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
        except FileNotFoundError as exc:
            self._events.append(
                self._envelope(
                    EventKind.ERROR,
                    {"code": "copilot_spawn_error", "message": str(exc)},
                )
            )
            self._emit_terminal("error")
            return
        except Exception as exc:  # noqa: BLE001 - backend boundary
            self._events.append(
                self._envelope(
                    EventKind.ERROR,
                    {
                        "code": "copilot_spawn_error",
                        "message": f"{type(exc).__name__}: {exc}",
                    },
                )
            )
            self._emit_terminal("error")
            return

        # Stream stdout line-by-line under the total-timeout deadline. Each
        # line is one JSON event in the CLI's envelope format.
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        timed_out = False
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                timed_out = True
                break
            try:
                line = await asyncio.wait_for(
                    proc.stdout.readline(), timeout=remaining
                )
            except asyncio.TimeoutError:
                timed_out = True
                break
            if not line:
                break
            text = line.decode("utf-8", errors="replace").strip()
            if not text:
                continue
            try:
                evt = json.loads(text)
            except json.JSONDecodeError:
                logger.warning("copilot: dropped non-JSON line: %r", text[:200])
                continue
            if isinstance(evt, dict):
                self._events.extend(self._translate_event(evt))

        if timed_out:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            await proc.wait()
            self._close_transport(proc)
            self._events.append(
                self._envelope(
                    EventKind.ERROR,
                    {
                        "code": "copilot_timeout",
                        "message": f"copilot exceeded {timeout:.1f}s",
                    },
                )
            )
            self._emit_terminal("timeout")
            return

        # Drain stderr (the Go port folds its tail into failure diagnostics).
        stderr_text = ""
        try:
            stderr_bytes = await asyncio.wait_for(
                proc.stderr.read(), timeout=5.0
            )
            stderr_text = stderr_bytes.decode("utf-8", errors="replace").strip()
        except Exception:  # noqa: BLE001 - diagnostics only
            stderr_text = ""

        try:
            rc = await asyncio.wait_for(proc.wait(), timeout=15.0)
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            rc = await proc.wait()
        self._close_transport(proc)

        # Compose the final failure, mirroring Go's withCopilotExitCode:
        # a protocol error keeps its ERROR envelope; a raw non-zero exit
        # gets its own (stderr tail appended for operator diagnostics).
        reason = "success"
        exit_msg = f"copilot exited with code {rc}"
        if self._protocol_error is not None:
            reason = "error"
            if rc != 0 and exit_msg not in self._protocol_error:
                logger.warning(
                    "copilot: %s; %s", self._protocol_error, exit_msg
                )
        elif rc != 0:
            reason = "error"
            message = exit_msg
            if stderr_text:
                message = f"{message}; stderr={stderr_text[:500]}"
            self._events.append(
                self._envelope(
                    EventKind.ERROR,
                    {"code": "copilot_exit", "message": message},
                )
            )

        self._emit_terminal(reason)

    def _close_transport(self, proc: asyncio.subprocess.Process) -> None:
        """Close the subprocess transport while the loop is still running.

        asyncio never closes a subprocess transport on its own; one left
        to the GC is finalized after the event loop has shut down and
        surfaces as a pytest unraisable-exception warning.
        """
        transport = getattr(proc, "_transport", None)
        if transport is not None:
            try:
                transport.close()
            except Exception:  # noqa: BLE001 - best-effort cleanup
                pass

    def _emit_terminal(self, reason: str) -> None:
        """Emit TURN_COMPLETE + SESSION_COMPLETE (stub lifecycle shape)."""
        self._events.append(
            self._envelope(EventKind.TURN_COMPLETE, {"reason": reason})
        )
        payload: dict[str, Any] = {"reason": reason}
        if self._native_session_id:
            payload["session_id"] = self._native_session_id
        self._events.append(
            self._envelope(EventKind.SESSION_COMPLETE, payload)
        )

    # ------------------------------------------------------------------
    # Event translation
    # ------------------------------------------------------------------

    def _translate_event(self, evt: dict[str, Any]) -> list[EventEnvelope]:
        """Translate one Copilot JSONL event into EventEnvelopes.

        Ports multica's ``handleCopilotEvent`` switch. Returns an empty
        list for events with no transcript content.
        """
        etype = str(evt.get("type", ""))
        data = evt.get("data")
        if not isinstance(data, dict):
            data = {}

        if etype == "assistant.message_delta":
            return self._translate_message_delta(data)
        if etype == "assistant.message":
            return self._translate_assistant_message(data)
        if etype == "tool.execution_complete":
            return [self._translate_tool_complete(data)]
        if etype in ("assistant.reasoning", "assistant.reasoning_delta"):
            text = str(data.get("content") or data.get("deltaContent") or "")
            if text:
                return [self._thinking_envelope(text)]
            return []
        if etype == "session.start":
            # Capture sessionId here too: the synthetic "result" event may
            # never arrive (timeout / crash), and result still wins when
            # it does (copilot.go does the same).
            sid = str(data.get("sessionId") or "")
            if sid:
                self._native_session_id = sid
            return []
        if etype == "session.error":
            message = str(data.get("message") or "copilot session error")
            if self._protocol_error is None:
                self._protocol_error = message
            return [
                self._envelope(
                    EventKind.ERROR,
                    {"code": "copilot_session_error", "message": message},
                )
            ]
        if etype == "result":
            # Synthetic terminal line: top-level fields, not under "data".
            sid = str(evt.get("sessionId") or "")
            if sid:
                self._native_session_id = sid
            code = evt.get("exitCode")
            if isinstance(code, int) and code != 0 and self._protocol_error is None:
                self._protocol_error = f"copilot exited with code {code}"
            return []
        if etype in ("assistant.turn_start", "assistant.usage", "session.shutdown"):
            # Status / token-usage only — no orchestratord transcript slot.
            return []
        return [
            self._envelope(
                EventKind.UNKNOWN, {"event": etype, "raw": dict(evt)}
            )
        ]

    def _translate_message_delta(self, data: dict[str, Any]) -> list[EventEnvelope]:
        delta = str(data.get("deltaContent") or "")
        if not delta:
            return []
        self._turn_deltas = True
        return [self._envelope(EventKind.TEXT_DELTA, {"text": delta})]

    def _translate_assistant_message(
        self, data: dict[str, Any]
    ) -> list[EventEnvelope]:
        """Translate ``assistant.message``: thinking, tools, turn text.

        ``content`` is the authoritative turn text. When deltas already
        streamed this turn it is NOT re-emitted (the chat bridge folds
        TEXT/TEXT_DELTA, so a re-emit would duplicate the answer); when
        no deltas arrived, it is the only copy the transcript gets.
        """
        out: list[EventEnvelope] = []
        reasoning = str(data.get("reasoningText") or "")
        if reasoning:
            out.append(self._thinking_envelope(reasoning))
        content = str(data.get("content") or "")
        if content and not self._turn_deltas:
            out.append(
                self._envelope(
                    EventKind.TEXT,
                    {"text": content, "native_type": "assistant.message"},
                )
            )
        for tr in data.get("toolRequests") or []:
            if not isinstance(tr, dict):
                continue
            arguments: Any = tr.get("arguments")
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments)
                except json.JSONDecodeError:
                    arguments = {"raw": arguments}
            if not isinstance(arguments, dict):
                arguments = {}
            out.append(
                self._envelope(
                    EventKind.TOOL_CALL,
                    {
                        "call_id": str(tr.get("toolCallId") or ""),
                        "name": str(tr.get("name") or ""),
                        "arguments": arguments,
                        "session_id": self._native_session_id,
                    },
                )
            )
        # assistant.message IS the turn boundary: reset the delta guard so
        # a content-empty tool turn cannot suppress the next turn's text.
        self._turn_deltas = False
        return out

    def _translate_tool_complete(self, data: dict[str, Any]) -> EventEnvelope:
        """Translate ``tool.execution_complete`` (copilot.go verbatim)."""
        success = bool(data.get("success"))
        if success:
            result = data.get("result") or {}
            output = str(result.get("content") or "") if isinstance(result, dict) else ""
        else:
            error = data.get("error") or {}
            if isinstance(error, dict) and error.get("message"):
                output = f"Error: {error['message']}"
            else:
                result = data.get("result") or {}
                output = str(result.get("content") or "") if isinstance(result, dict) else ""
        return self._envelope(
            EventKind.TOOL_RESULT,
            {"call_id": str(data.get("toolCallId") or ""), "ok": success, "output": output},
        )

    def _thinking_envelope(self, text: str) -> EventEnvelope:
        return self._envelope(
            EventKind.UNKNOWN,
            {"thinking": text, "session_id": self._native_session_id},
        )

    # ------------------------------------------------------------------
    # Iterator API
    # ------------------------------------------------------------------

    async def _emit_events(self) -> AsyncIterator[EventEnvelope]:
        for ev in self._events:
            yield ev
        self._events.clear()

    def events(self) -> AsyncIterator[EventEnvelope]:
        return self._emit_events()

    # ------------------------------------------------------------------
    # No-op protocol stubs
    # ------------------------------------------------------------------

    async def interrupt(self) -> None:
        # One-shot CLI runs have no interrupt channel; the orchestrator's
        # graceful-shutdown path is the supported way to abort.
        return None

    async def approve(self, request_id: str, decision: ApprovalDecision) -> None:
        # The CLI runs with --allow-all --no-ask-user (full headless mode).
        return None

    async def probe_resume(self) -> ResumeStatus:
        """copilot has no cross-process resume probe."""
        if not self._spec.resume_session_id:
            return ResumeStatus.RESUMED
        return ResumeStatus.UNDETECTABLE

    async def close(self) -> None:
        self._closed = True
