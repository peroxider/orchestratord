"""OpenclawSession — wraps ``openclaw`` into the AgentSession SPI.

Spawn-per-turn Cli model: ``send`` runs the subprocess to completion and
buffers the entire stdout as a single TEXT event. §8.1 marks openclaw's
native protocol as HTTP (``openclaw agent --local`` vs Gateway routing);
that wire path is deferred, so this session is intentionally conservative
and does not yet claim ``streaming_deltas``.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from typing import Any

from orchestratord.spi.approval import ApprovalDecision
from orchestratord.spi.backend import SessionSpec
from orchestratord.spi.capabilities import BackendCapabilities
from orchestratord.spi.events import EventEnvelope, EventKind
from orchestratord.spi.session import ResumeStatus

_DEFAULT_TOTAL_TIMEOUT_S = 600.0


class OpenclawSession:
    """Adapt ``openclaw`` subprocess output into the AgentSession SPI."""

    def __init__(self, spec: SessionSpec) -> None:
        self._spec = spec
        self.session_id = spec.resume_session_id or f"openclaw-{id(self)}"
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

    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq

    def _now(self) -> float:
        return time.time()

    async def send(self, content: str | list[Any]) -> None:
        if self._closed:
            raise RuntimeError("session closed")

        text = content if isinstance(content, str) else str(content)

        args = ["openclaw"]
        if self._spec.model:
            args.extend(["--model", self._spec.model])

        reason = "success"
        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                cwd=self._spec.cwd or None,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            timeout = self._spec.total_timeout_s or _DEFAULT_TOTAL_TIMEOUT_S
            try:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(input=text.encode("utf-8")),
                    timeout=timeout,
                )
            except asyncio.TimeoutError:
                reason = "timeout"
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
                await proc.wait()
                self._events.append(
                    EventEnvelope(
                        seq=self._next_seq(),
                        timestamp=self._now(),
                        kind=EventKind.ERROR,
                        payload={
                            "code": "openclaw_timeout",
                            "message": f"openclaw exceeded {timeout:.1f}s",
                        },
                    )
                )
                self._events.append(
                    EventEnvelope(
                        seq=self._next_seq(),
                        timestamp=self._now(),
                        kind=EventKind.TURN_COMPLETE,
                        payload={"reason": reason},
                    )
                )
                self._events.append(
                    EventEnvelope(
                        seq=self._next_seq(),
                        timestamp=self._now(),
                        kind=EventKind.SESSION_COMPLETE,
                        payload={"reason": reason},
                    )
                )
                return

            if proc.returncode != 0 and not stdout:
                reason = "error"
                stderr_text = stderr.decode("utf-8", errors="replace").strip()
                self._events.append(
                    EventEnvelope(
                        seq=self._next_seq(),
                        timestamp=self._now(),
                        kind=EventKind.ERROR,
                        payload={
                            "code": "openclaw_exit",
                            "message": (
                                f"openclaw exited with code {proc.returncode}; "
                                f"stderr={stderr_text[:500]}"
                            ),
                        },
                    )
                )
            else:
                output = stdout.decode("utf-8", errors="replace").strip()
                if output:
                    self._events.append(
                        EventEnvelope(
                            seq=self._next_seq(),
                            timestamp=self._now(),
                            kind=EventKind.TEXT,
                            payload={
                                "text": output,
                                "native_type": "stdout",
                            },
                        )
                    )
        except FileNotFoundError as exc:
            reason = "error"
            self._events.append(
                EventEnvelope(
                    seq=self._next_seq(),
                    timestamp=self._now(),
                    kind=EventKind.ERROR,
                    payload={"code": "openclaw_spawn_error", "message": str(exc)},
                )
            )
        except Exception as exc:  # noqa: BLE001 - backend boundary
            reason = "error"
            self._events.append(
                EventEnvelope(
                    seq=self._next_seq(),
                    timestamp=self._now(),
                    kind=EventKind.ERROR,
                    payload={
                        "code": "openclaw_error",
                        "message": f"{type(exc).__name__}: {exc}",
                    },
                )
            )

        self._events.append(
            EventEnvelope(
                seq=self._next_seq(),
                timestamp=self._now(),
                kind=EventKind.TURN_COMPLETE,
                payload={"reason": reason},
            )
        )
        self._events.append(
            EventEnvelope(
                seq=self._next_seq(),
                timestamp=self._now(),
                kind=EventKind.SESSION_COMPLETE,
                payload={"reason": reason},
            )
        )

    async def _emit_events(self) -> AsyncIterator[EventEnvelope]:
        for ev in self._events:
            yield ev
        self._events.clear()

    def events(self) -> AsyncIterator[EventEnvelope]:
        return self._emit_events()

    async def interrupt(self) -> None:
        return None

    async def approve(self, request_id: str, decision: ApprovalDecision) -> None:
        return None

    async def probe_resume(self) -> ResumeStatus:
        """openclaw has no cross-process resume probe."""
        if not self._spec.resume_session_id:
            return ResumeStatus.RESUMED
        return ResumeStatus.UNDETECTABLE

    async def close(self) -> None:
        self._closed = True
