"""ClaudeSession — spawn the Claude Code CLI and parse its stream-json output.

Protocol summary (``claude -p --output-format stream-json --verbose``)
======================================================================

Each stdout line is a JSON object with a ``type`` discriminator. Observed
event types we translate (other types are logged but not surfaced):

* ``system``        — init / hook lifecycle. Ignored (hook noise).
* ``user``          — user turn with content blocks (text or tool_result).
* ``assistant``     — assistant message with content blocks (text, tool_use, thinking).
* ``result``        — terminal event with ``total_cost_usd``, ``duration_ms``, etc.

Translation table → :class:`EventEnvelope` / :class:`EventKind`:

* assistant ``text`` block          → ``TEXT_DELTA``
* assistant ``tool_use`` block      → ``TOOL_CALL``
* user    ``tool_result`` block     → ``TOOL_RESULT``
* assistant ``thinking`` block      → ``UNKNOWN`` with a raw/reasoning payload
* ``result``                        → ``TURN_COMPLETE`` + ``SESSION_COMPLETE`` with cost
* spawn / parse / CLI-error         → ``ERROR``

Failure model: a non-zero exit OR a JSON ``is_error: true`` result both
yield a ``SESSION_COMPLETE`` with ``reason='error'``. Stderr is captured
verbatim into the ``ERROR`` event payload so operators can diagnose
auth/network issues without re-running the CLI.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator
from typing import Any

from orchestratord.spi.approval import ApprovalDecision
from orchestratord.spi.backend import SessionSpec
from orchestratord.spi.capabilities import BackendCapabilities
from orchestratord.spi.events import EventEnvelope, EventKind
from orchestratord.spi.session import ResumeStatus

logger = logging.getLogger(__name__)


# Default per-call timeout if SessionSpec.total_timeout_s is unset. The CLI
# can take a few minutes for tool-using turns; this is a worst-case guard.
_DEFAULT_TOTAL_TIMEOUT_S = 900.0


def _looks_like_claude_session_id(value: str) -> bool:
    """Return True iff *value* could be a Claude-side session id/title.

    The CLI accepts either a UUID (e.g. ``d45352b8-316b-4cd0-be83-990f28602d3f``)
    or a user-visible session title. The orchestrator's run_id looks like
    ``20260828_141948_ccb-smoke-001`` — clearly not a UUID and not a title,
    so passing it as ``--resume`` makes the CLI exit non-zero. We filter
    those out and let the CLI start a fresh session instead.
    """
    # UUID-ish: 8-4-4-4-12 hex
    if len(value) == 36 and value.count("-") == 4:
        return True
    # Short slug (Claude session title fallback) — alphanumeric + dashes,
    # length 4..64. We intentionally keep this loose; the CLI will reject
    # the rare false positive with a clear error rather than silently
    # dropping the user's request.
    if 4 <= len(value) <= 64 and all(c.isalnum() or c == "-" for c in value):
        return True
    return False


class ClaudeSession:
    """Adapts a single ``claude -p`` invocation into an AgentSession."""

    def __init__(self, spec: SessionSpec, *, binary: str) -> None:
        self._spec = spec
        self._binary = binary  # absolute path to claude or ccb
        # session_id is reused as Claude's ``--resume`` target if available;
        # otherwise we generate a stable opaque id from the spec.
        self.session_id = (
            spec.resume_session_id or f"claude-{time.time_ns()}-{id(self)}"
        )
        self.capabilities = BackendCapabilities(
            streaming_deltas=True,
            resumable=True,
            interrupt=False,
            approval_hooks=False,
            parallel_sessions=True,
            cost_reporting=True,
            tool_filtering=False,
            takeover=False,
        )
        self._events: list[EventEnvelope] = []
        self._seq = 0
        self._closed = False
        # Live handle to the current turn's subprocess (set by send(), cleared
        # once it is reaped) so an operator can pause/resume/stop the run.
        self._current_proc: asyncio.subprocess.Process | None = None

    @property
    def current_pid(self) -> int | None:
        """PID of the in-flight ``claude -p`` child, or ``None`` between turns."""
        return self._current_proc.pid if self._current_proc is not None else None

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq

    def _now(self) -> float:
        return time.time()

    def _build_argv(self) -> list[str]:
        """Build the argv for one ``claude -p`` invocation."""
        spec = self._spec
        args: list[str] = [
            self._binary,
            "-p",
            "--output-format", "stream-json",
            "--verbose",
            # Always bypass permission prompts in unattended daemon context.
            # Operators who need stricter sandboxing should run with a
            # restricted SessionSpec.cwd rather than rely on CLI prompts.
            "--dangerously-skip-permissions",
        ]
        if spec.model:
            args.extend(["--model", spec.model])
        # Grant the CLI read/write access to the issue workspace.
        if spec.cwd:
            args.extend(["--add-dir", spec.cwd])
        # Only pass ``--resume`` when the id looks like a Claude-side session
        # (UUID or short slug). The orchestrator's ``run_id`` is not a
        # Claude id — passing it makes the CLI exit with code 1.
        if spec.resume_session_id and _looks_like_claude_session_id(
            spec.resume_session_id
        ):
            args.extend(["--resume", spec.resume_session_id])
        # ``--append-system-prompt`` keeps the CLI's default system prompt
        # and layers ours on top — safer than replacing it wholesale.
        if spec.system_prompt:
            args.extend(["--append-system-prompt", spec.system_prompt])
        return args

    # ------------------------------------------------------------------
    # AgentSession Protocol
    # ------------------------------------------------------------------

    async def send(self, content: str | list[Any]) -> None:
        if self._closed:
            raise RuntimeError("session closed")

        prompt = content if isinstance(content, str) else str(content)
        argv = self._build_argv()
        timeout = self._spec.total_timeout_s or _DEFAULT_TOTAL_TIMEOUT_S
        env = dict(self._spec.env) if self._spec.env else None

        stderr_text = ""
        error_emitted = False
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
            # The preflight gate should have caught this; surface anyway.
            self._emit_error("claude_spawn_error", f"{exc}")
            await self._emit_terminal(reason="error")
            return
        except Exception as exc:
            self._emit_error(
                "claude_spawn_error", f"{type(exc).__name__}: {exc}"
            )
            await self._emit_terminal(reason="error")
            return
        self._current_proc = proc

        # Pipe the prompt in via stdin (CLI reads it then runs non-interactively).
        assert proc.stdin is not None
        try:
            proc.stdin.write(prompt.encode("utf-8"))
            await proc.stdin.drain()
        except Exception as exc:
            self._emit_error(
                "claude_stdin_error", f"{type(exc).__name__}: {exc}"
            )
            error_emitted = True
        finally:
            try:
                proc.stdin.close()
            except Exception:
                pass

        # Stream stdout line-by-line; each line is one JSON event.
        assert proc.stdout is not None
        result_payload: dict[str, Any] | None = None
        try:
            while True:
                line = await proc.stdout.readline()
                if not line:
                    break
                text = line.decode("utf-8", errors="replace").strip()
                if not text:
                    continue
                try:
                    evt = json.loads(text)
                except json.JSONDecodeError:
                    logger.warning(
                        "claude: dropped non-JSON line: %r", text[:200]
                    )
                    continue
                translated = self._translate_event(evt)
                self._events.extend(translated)
                # Stash the final result for the terminal envelope.
                if isinstance(evt, dict) and evt.get("type") == "result":
                    result_payload = evt
        except Exception as exc:
            self._emit_error(
                "claude_stream_error", f"{type(exc).__name__}: {exc}"
            )
            error_emitted = True

        # Drain stderr + wait for clean exit.
        assert proc.stderr is not None
        try:
            stderr_bytes = await asyncio.wait_for(proc.stderr.read(), timeout=5.0)
            stderr_text = stderr_bytes.decode("utf-8", errors="replace").strip()
        except Exception:
            stderr_text = ""

        try:
            rc = await asyncio.wait_for(proc.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            self._emit_error(
                "claude_timeout",
                f"claude CLI did not exit within {timeout}s",
            )
            error_emitted = True
            rc = -1
        finally:
            # Reaped (or killed) — the operator-control handle is dead.
            self._current_proc = None

        # The CLI reports API/tool failures in the ``result`` envelope while
        # also exiting non-zero. Check the envelope first — ``stderr`` is
        # typically empty in that case, so the exit-code message alone would
        # tell the operator nothing.
        if result_payload and result_payload.get("is_error") is True:
            self._emit_error(
                "claude_result_error",
                str(result_payload.get("result", "unspecified error")),
            )
            error_emitted = True

        if rc != 0 and not error_emitted:
            self._emit_error(
                "claude_exit",
                f"claude CLI exited with code {rc}; stderr={stderr_text[:500]}",
            )
            error_emitted = True

        await self._emit_terminal(
            reason="error" if error_emitted else "success",
            result_payload=result_payload,
        )

    async def _emit_terminal(
        self,
        *,
        reason: str,
        result_payload: dict[str, Any] | None = None,
    ) -> None:
        """Emit TURN_COMPLETE + SESSION_COMPLETE with cost/duration if known."""
        self._events.append(
            EventEnvelope(
                seq=self._next_seq(),
                timestamp=self._now(),
                kind=EventKind.TURN_COMPLETE,
                payload={"reason": reason},
            )
        )
        payload: dict[str, Any] = {"reason": reason}
        if result_payload:
            for key in (
                "total_cost_usd",
                "duration_ms",
                "duration_api_ms",
                "num_turns",
                "session_id",
            ):
                if key in result_payload:
                    payload[key] = result_payload[key]
        self._events.append(
            EventEnvelope(
                seq=self._next_seq(),
                timestamp=self._now(),
                kind=EventKind.SESSION_COMPLETE,
                payload=payload,
            )
        )

    # ------------------------------------------------------------------
    # Event translation
    # ------------------------------------------------------------------

    def _emit_error(self, code: str, message: str) -> None:
        self._events.append(
            EventEnvelope(
                seq=self._next_seq(),
                timestamp=self._now(),
                kind=EventKind.ERROR,
                payload={"code": code, "message": message},
            )
        )

    def _translate_event(self, evt: dict[str, Any]) -> list[EventEnvelope]:
        """Translate one stream-json event into EventEnvelopes.

        Returns an empty list for event types we don't surface (e.g.
        ``system`` init/hook noise). An assistant message may yield both
        a TEXT_DELTA and multiple TOOL_CALL envelopes — the caller extends
        the session event list with whatever this returns.
        """
        etype = evt.get("type", "")
        if etype == "assistant":
            return self._translate_assistant(evt)
        if etype == "user":
            return self._translate_user(evt)
        # Keep system and future provider events auditable.  They are
        # intentionally marked UNKNOWN so renderers can show a collapsed raw
        # event without mistaking it for user-visible assistant text.
        return [
            EventEnvelope(
                seq=self._next_seq(),
                timestamp=self._now(),
                kind=EventKind.UNKNOWN,
                payload={"event": etype, "raw": dict(evt)},
            )
        ]

    def _translate_assistant(self, evt: dict[str, Any]) -> list[EventEnvelope]:
        """Translate an assistant message into TEXT_DELTA / TOOL_CALL events."""
        message = evt.get("message", {})
        content = (
            message.get("content", []) if isinstance(message, dict) else []
        )
        if not isinstance(content, list):
            return []

        out: list[EventEnvelope] = []
        text_parts: list[str] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "text":
                text_parts.append(str(block.get("text", "")))
            elif btype == "tool_use":
                out.append(
                    EventEnvelope(
                        seq=self._next_seq(),
                        timestamp=self._now(),
                        kind=EventKind.TOOL_CALL,
                        payload={
                            "call_id": str(block.get("id", "")),
                            "name": str(block.get("name", "")),
                            "arguments": block.get("input", {}) or {},
                            "session_id": evt.get("session_id"),
                        },
                    )
                )
            elif btype == "thinking":
                out.append(
                    EventEnvelope(
                        seq=self._next_seq(),
                        timestamp=self._now(),
                        kind=EventKind.UNKNOWN,
                        payload={
                            "thinking": str(block.get("thinking", "")),
                            "session_id": evt.get("session_id"),
                            "raw": dict(block),
                        },
                    )
                )

        if text_parts:
            out.insert(
                0,
                EventEnvelope(
                    seq=self._next_seq(),
                    timestamp=self._now(),
                    kind=EventKind.TEXT_DELTA,
                    payload={"text": "".join(text_parts)},
                ),
            )
        return out

    def _translate_user(self, evt: dict[str, Any]) -> list[EventEnvelope]:
        """Translate a user turn — pick out ``tool_result`` blocks."""
        message = evt.get("message", {})
        content = (
            message.get("content", []) if isinstance(message, dict) else []
        )
        if not isinstance(content, list):
            return []
        out: list[EventEnvelope] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_result":
                # ``content`` may be a string or a list of text blocks.
                output = block.get("content", "")
                if isinstance(output, list):
                    output = "".join(
                        str(b.get("text", ""))
                        for b in output
                        if isinstance(b, dict) and b.get("type") == "text"
                    )
                is_err = bool(block.get("is_error", False))
                out.append(
                    EventEnvelope(
                        seq=self._next_seq(),
                        timestamp=self._now(),
                        kind=EventKind.TOOL_RESULT,
                        payload={
                            "call_id": str(block.get("tool_use_id", "")),
                            "ok": not is_err,
                            "output": str(output),
                        },
                    )
                )
        return out

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
        # The CLI doesn't expose an interrupt hook; the orchestrator's
        # graceful-shutdown path is the supported way to abort.
        pass

    async def approve(self, request_id: str, decision: ApprovalDecision) -> None:
        # No approval channel — the CLI runs with --dangerously-skip-permissions.
        pass

    async def probe_resume(self) -> ResumeStatus:
        # Claude doesn't currently support a cross-process
        # resume probe — return UNDETECTABLE so the orchestrator falls
        # through to its best-effort replay path.
        if not self._spec.resume_session_id:
            return ResumeStatus.RESUMED
        return ResumeStatus.UNDETECTABLE

    async def close(self) -> None:
        self._closed = True
