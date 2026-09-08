"""CursorSession — spawn ``cursor-agent`` and translate its stream-json output.

Protocol summary (``cursor-agent -p --output-format stream-json``)
==================================================================

One-shot spawn-per-turn invocation ported from the multica Go reference
(``server/pkg/agent/cursor.go``):

    cursor-agent -p --output-format stream-json --yolo
                 [--workspace <cwd>] [--model <model>] [--resume <id>]
                 [custom args]

``-p`` is a boolean print-mode switch. The prompt is deliberately NOT on
the command line: with no positional prompt and a non-TTY stdin the CLI
reads stdin to EOF and uses that as the prompt (the Go port relies on
this to keep user text off every shell command line). Events arrive as
newline-delimited JSON on stdout with a ``type`` discriminator, similar
to Claude Code's stream-json:

    { "type": "assistant" | "tool_call" | "result" | ..., ... }

Lines may be prefixed with ``stdout:``/``stderr:``; the prefix is
stripped before parsing (Go ``normalizeCursorStreamLine``).

Translation table → :class:`EventEnvelope` / :class:`EventKind`:

* ``assistant`` ``text``/``output_text`` block → ``TEXT`` (complete block;
  stream-json has no incremental text-fragment event, so the backend does
  NOT claim ``streaming_deltas``)
* ``assistant`` ``tool_use`` block    → ``TOOL_CALL``
* ``assistant`` ``thinking`` block    → ``UNKNOWN`` ``{"thinking": ...}``
* ``thinking`` ``subtype:"delta"``    → ``UNKNOWN`` ``{"thinking": ...}``
  (``subtype:"completed"`` closes the block — nothing forwarded)
* ``tool_call`` ``subtype:"started"`` → ``TOOL_CALL`` (name decoded from the
  nested ``<name>ToolCall`` envelope key; ``call_id`` cut at the embedded
  newline per Go ``cursorCallID``)
* ``tool_call`` ``subtype:"completed"`` → ``TOOL_RESULT`` (``result`` payload
  serialized as text)
* ``tool_use`` / ``tool_result`` (legacy flat events) → ``TOOL_CALL`` /
  ``TOOL_RESULT``
* ``result``                          → ``TURN_COMPLETE`` + ``SESSION_COMPLETE``
  (``result`` text fills in as TEXT only when nothing streamed); the
  result event is the protocol boundary — a non-zero child exit after it
  is a lingering worker, not a failure
* ``system`` ``subtype:"error"`` / ``error`` → ``ERROR`` + terminal
  ``reason="error"``
* ``user`` / ``connection`` / ``retry`` (known non-transcript control
  frames) and usage-only events → dropped; anything else unrecognized →
  ``UNKNOWN`` (auditable, per the claude backend's convention)

Token usage (``result`` totals / ``step_finish``) is accumulated into
``Result.Usage`` by the Go reference, but the orchestratord SPI has no
usage channel and ``cost_reporting`` stays False, so it is not translated.

Failure model: with a parsed ``result`` the run is ``success`` (or
``error`` when ``is_error``/``subtype=="error"``). Without one: timeout →
``reason="timeout"``, a protocol error → ``reason="error"`` (its ERROR
envelope already emitted), otherwise the exit code / a missing terminal
result produces ``ERROR`` and ``reason="error"``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
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

# cursor-agent may prefix stream lines with "stdout:" / "stderr:".
_CURSOR_STREAM_PREFIX_RE = re.compile(r"^(stdout|stderr)\s*[:=]?\s*", re.IGNORECASE)

_TOOL_CALL_KEY_SUFFIX = "ToolCall"


class CursorSession:
    """Adapt the ``cursor-agent`` stream-json stream into the AgentSession SPI."""

    # Flags the daemon hardcodes; user-configured custom args must never
    # override them (multica cursor.go ``cursorBlockedArgs``).
    _BLOCKED_STANDALONE = frozenset({"-p", "--yolo"})
    _BLOCKED_WITH_VALUE = frozenset({"--output-format"})

    def __init__(self, spec: SessionSpec) -> None:
        self._spec = spec
        self.session_id = spec.resume_session_id or f"cursor-{id(self)}"
        self.capabilities = BackendCapabilities(
            streaming_deltas=False,
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
        self._result_seen = False
        self._output = ""

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
        """Resolve the CLI binary: explicit override → ``cursor-agent``."""
        return (self._spec.runtime_bin or "").strip() or "cursor-agent"

    def _build_argv(self) -> list[str]:
        """Build the argv for one ``cursor-agent -p`` invocation.

        Fixed, content-free flags only — the prompt rides on stdin.
        """
        binary = self._binary()
        prefix: list[str] = []
        if binary.endswith(".py"):
            # A runtime_bin pointing at a Python script is launched through
            # the running interpreter — how the test suite wires the
            # scripted fake CLI (tests/_fake_cursor_cli.py) in place of
            # the real binary.
            prefix = [sys.executable]
        args = [*prefix, binary, "-p", "--output-format", "stream-json", "--yolo"]
        if self._spec.cwd:
            args.extend(["--workspace", self._spec.cwd])
        if self._spec.model:
            args.extend(["--model", self._spec.model])
        if self._spec.resume_session_id:
            args.extend(["--resume", self._spec.resume_session_id])
        args.extend(self._filter_custom_args())
        return args

    def _filter_custom_args(self) -> list[str]:
        """Append user-configured custom args minus the daemon's flags.

        Mirrors multica's ``filterCustomArgs`` + ``cursorBlockedArgs``.
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
        argv = self._build_argv()
        timeout = self._spec.total_timeout_s or _DEFAULT_TOTAL_TIMEOUT_S
        env = {**os.environ, **self._spec.env} if self._spec.env else None

        self._native_session_id = None
        self._protocol_error = None
        self._result_seen = False
        self._output = ""

        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                cwd=self._spec.cwd or None,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
        except FileNotFoundError as exc:
            self._events.append(
                self._envelope(
                    EventKind.ERROR,
                    {"code": "cursor_spawn_error", "message": str(exc)},
                )
            )
            self._emit_terminal("error")
            return
        except Exception as exc:  # noqa: BLE001 - backend boundary
            self._events.append(
                self._envelope(
                    EventKind.ERROR,
                    {
                        "code": "cursor_spawn_error",
                        "message": f"{type(exc).__name__}: {exc}",
                    },
                )
            )
            self._emit_terminal("error")
            return

        # The prompt is delivered on stdin and closing stdin is the
        # end-of-prompt signal. Write it from its own task so a prompt
        # larger than the OS pipe buffer cannot deadlock against the
        # stdout reader below (mirrors the Go port's writer goroutine).
        assert proc.stdin is not None
        write_error: Exception | None = None

        async def _write_prompt() -> None:
            nonlocal write_error
            try:
                proc.stdin.write(prompt.encode("utf-8"))
                await proc.stdin.drain()
            except Exception as exc:  # noqa: BLE001 - reported below
                write_error = exc
            finally:
                try:
                    proc.stdin.close()
                except Exception:  # noqa: BLE001 - best effort
                    pass

        writer = asyncio.create_task(_write_prompt())

        # Stream stdout line-by-line under the total-timeout deadline.
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
            text = self._normalize_stream_line(
                line.decode("utf-8", errors="replace")
            )
            if not text:
                continue
            try:
                evt = json.loads(text)
            except json.JSONDecodeError:
                logger.warning("cursor: dropped non-JSON line: %r", text[:200])
                continue
            if isinstance(evt, dict):
                self._events.extend(self._translate_event(evt))

        try:
            await asyncio.wait_for(writer, timeout=5.0)
        except asyncio.TimeoutError:
            writer.cancel()

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
                        "code": "cursor_timeout",
                        "message": f"cursor-agent exceeded {timeout:.1f}s",
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

        reason = self._resolve_reason(rc, stderr_text, write_error)
        self._emit_terminal(reason)

    def _resolve_reason(
        self, rc: int, stderr_text: str, write_error: Exception | None
    ) -> str:
        """Post-stream lifecycle, mirroring the Go port's failure ranking.

        A parsed ``result`` is the protocol boundary: a lingering cursor
        worker's non-zero exit or our own cancellation is ignored.
        """
        if self._result_seen:
            if self._protocol_error is not None:
                return "error"
            return "success"

        # No result event: rank failures like cursor.go does.
        if write_error is not None and self._protocol_error is None:
            self._events.append(
                self._envelope(
                    EventKind.ERROR,
                    {
                        "code": "cursor_stdin_error",
                        "message": (
                            f"cursor-agent prompt write failed: "
                            f"{type(write_error).__name__}: {write_error}"
                        ),
                    },
                )
            )
            return "error"
        if self._protocol_error is not None:
            # Its ERROR envelope was already emitted mid-stream.
            return "error"
        if rc != 0:
            message = f"cursor-agent exited with code {rc}"
            if stderr_text:
                message = f"{message}; stderr={stderr_text[:500]}"
            self._events.append(
                self._envelope(
                    EventKind.ERROR,
                    {"code": "cursor_exit", "message": message},
                )
            )
            return "error"
        self._events.append(
            self._envelope(
                EventKind.ERROR,
                {
                    "code": "cursor_no_result",
                    "message": "cursor-agent stream ended without terminal result",
                },
            )
        )
        return "error"

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

    def _normalize_stream_line(self, raw: str) -> str:
        """Strip the ``stdout:``/``stderr:`` prefix cursor-agent may emit."""
        trimmed = raw.strip()
        if not trimmed:
            return ""
        return _CURSOR_STREAM_PREFIX_RE.sub("", trimmed).strip()

    def _translate_event(self, evt: dict[str, Any]) -> list[EventEnvelope]:
        """Translate one stream-json event into EventEnvelopes.

        Ports multica's ``cursorBackend`` switch. Returns an empty list
        for events with no transcript content.
        """
        etype = str(evt.get("type", ""))
        subtype = str(evt.get("subtype", ""))

        sid = str(evt.get("session_id") or "").strip()
        if sid:
            self._native_session_id = sid

        if etype == "system":
            if subtype == "error":
                return self._translate_protocol_error(evt)
            return []
        if etype == "assistant":
            return self._translate_assistant(evt)
        if etype == "thinking":
            if subtype == "delta":
                text = str(evt.get("text") or "")
                if text:
                    return [self._thinking_envelope(text)]
            return []
        if etype == "tool_call":
            return self._translate_tool_call_event(evt, subtype)
        if etype == "tool_use":
            params = evt.get("parameters")
            if not isinstance(params, dict):
                params = {}
            return [
                self._tool_call_envelope(
                    call_id=self._normalize_call_id(str(evt.get("tool_id") or "")),
                    name=str(evt.get("tool_name") or ""),
                    arguments=params,
                )
            ]
        if etype == "tool_result":
            return [
                self._envelope(
                    EventKind.TOOL_RESULT,
                    {
                        "call_id": self._normalize_call_id(
                            str(evt.get("tool_id") or "")
                        ),
                        "ok": True,
                        "output": str(evt.get("output") or ""),
                    },
                )
            ]
        if etype == "result":
            return self._translate_result(evt)
        if etype == "error":
            return self._translate_protocol_error(evt)
        if etype == "text":
            part = evt.get("part") or {}
            text = str(part.get("text") or "") if isinstance(part, dict) else ""
            if text:
                self._output += text
                return [
                    self._envelope(
                        EventKind.TEXT,
                        {"text": text, "native_type": "text"},
                    )
                ]
            return []
        if etype in ("step_finish",):
            # Token usage only — no orchestratord transcript slot.
            return []
        if etype in ("user", "connection", "retry"):
            # Known non-transcript control frames (cursor.go keeps these
            # out of its unhandled-type diagnostic).
            return []
        return [
            self._envelope(
                EventKind.UNKNOWN, {"event": etype, "raw": dict(evt)}
            )
        ]

    def _translate_assistant(self, evt: dict[str, Any]) -> list[EventEnvelope]:
        """Forward one assistant event's content blocks (Go handleCursorAssistant)."""
        message = evt.get("message")
        if not isinstance(message, dict):
            return []
        content = message.get("content")
        if not isinstance(content, list):
            return []
        out: list[EventEnvelope] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = str(block.get("type", ""))
            if btype in ("output_text", "text"):
                text = str(block.get("text") or "")
                if text:
                    self._output += text
                    out.append(
                        self._envelope(
                            EventKind.TEXT,
                            {"text": text, "native_type": "assistant"},
                        )
                    )
            elif btype == "thinking":
                text = str(block.get("text") or "")
                if text:
                    out.append(self._thinking_envelope(text))
            elif btype == "tool_use":
                arguments: Any = block.get("input")
                if not isinstance(arguments, dict):
                    arguments = {}
                out.append(
                    self._tool_call_envelope(
                        call_id=str(block.get("id") or ""),
                        name=str(block.get("name") or ""),
                        arguments=arguments,
                    )
                )
        return out

    def _translate_tool_call_event(
        self, evt: dict[str, Any], subtype: str
    ) -> list[EventEnvelope]:
        """``tool_call`` started/completed drive the transcript; other
        subtypes must not synthesize anything (Go MUL-5231)."""
        call_id, name, arguments, result = self._parse_tool_call(evt)
        if subtype == "started":
            return [
                self._tool_call_envelope(
                    call_id=call_id, name=name, arguments=arguments
                )
            ]
        if subtype == "completed":
            return [
                self._envelope(
                    EventKind.TOOL_RESULT,
                    {"call_id": call_id, "ok": True, "output": result},
                )
            ]
        return []

    def _parse_tool_call(
        self, evt: dict[str, Any]
    ) -> tuple[str, str, dict[str, Any], str]:
        """Decode a ``tool_call`` event (Go parseCursorToolCall).

        The tool is a nested ``<name>ToolCall`` key rather than a ``name``
        field; a payload we cannot decode still yields the call ID so
        started/completed stay paired.
        """
        call_id = self._normalize_call_id(str(evt.get("call_id") or ""))
        envelope = evt.get("tool_call")
        if not isinstance(envelope, dict):
            return call_id, "", {}, ""
        if not call_id:
            call_id = self._normalize_call_id(
                str(envelope.get("toolCallId") or "")
            )
        key = self._tool_payload_key(envelope)
        if not key:
            return call_id, "", {}, ""
        name = key[: -len(_TOOL_CALL_KEY_SUFFIX)]
        payload = envelope[key]
        if not isinstance(payload, dict):
            return call_id, name, {}, ""
        args = payload.get("args")
        if not isinstance(args, dict):
            args = {}
        raw_result = payload.get("result")
        result = (
            json.dumps(raw_result, ensure_ascii=False)
            if raw_result is not None
            else ""
        )
        return call_id, name, args, result

    @staticmethod
    def _tool_payload_key(envelope: dict[str, Any]) -> str:
        keys = sorted(
            key
            for key in envelope
            if len(key) > len(_TOOL_CALL_KEY_SUFFIX)
            and key.endswith(_TOOL_CALL_KEY_SUFFIX)
        )
        return keys[0] if keys else ""

    @staticmethod
    def _normalize_call_id(raw: str) -> str:
        """Cut a packed ``call-…\\nfc_…`` id at the embedded newline."""
        call_id = raw.strip()
        if "\n" in call_id:
            call_id = call_id.split("\n", 1)[0]
        return call_id.strip()

    def _translate_result(self, evt: dict[str, Any]) -> list[EventEnvelope]:
        self._result_seen = True
        if bool(evt.get("is_error")) or str(evt.get("subtype", "")) == "error":
            if self._protocol_error is None:
                self._protocol_error = self._error_text(evt)
            return [
                self._envelope(
                    EventKind.ERROR,
                    {
                        "code": "cursor_result_error",
                        "message": self._protocol_error or "unknown error",
                    },
                )
            ]
        result_text = str(evt.get("result") or "")
        if result_text and not self._output:
            # Nothing streamed — the result text is the only copy.
            self._output += result_text
            return [
                self._envelope(
                    EventKind.TEXT,
                    {"text": result_text, "native_type": "result"},
                )
            ]
        return []

    def _translate_protocol_error(self, evt: dict[str, Any]) -> list[EventEnvelope]:
        message = self._error_text(evt) or "cursor-agent error"
        if self._protocol_error is None:
            self._protocol_error = message
        return [
            self._envelope(
                EventKind.ERROR,
                {"code": "cursor_error", "message": message},
            )
        ]

    @staticmethod
    def _error_text(evt: dict[str, Any]) -> str:
        """First of ``error`` / ``detail`` / ``result`` (Go cursorErrorText)."""
        for key in ("error", "detail", "result"):
            value = str(evt.get(key) or "")
            if value:
                return value
        return ""

    def _tool_call_envelope(
        self, call_id: str, name: str, arguments: dict[str, Any]
    ) -> EventEnvelope:
        return self._envelope(
            EventKind.TOOL_CALL,
            {
                "call_id": call_id,
                "name": name,
                "arguments": arguments,
                "session_id": self._native_session_id,
            },
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
        # The CLI runs with --yolo (auto-approval for autonomous operation).
        return None

    async def probe_resume(self) -> ResumeStatus:
        """cursor-agent has no cross-process resume probe.

        Per the §3.1 rule "backend 不得自降", we surface UNDETECTABLE so the
        orchestrator can choose to attempt send() anyway rather than silently
        collapsing the probe result.
        """
        if not self._spec.resume_session_id:
            return ResumeStatus.RESUMED
        return ResumeStatus.UNDETECTABLE

    async def close(self) -> None:
        self._closed = True
