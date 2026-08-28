"""HermesSession — wraps hermes CLI into the AgentSession SPI."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from typing import Any

from orchestratord.spi.approval import ApprovalDecision
from orchestratord.spi.capabilities import BackendCapabilities
from orchestratord.spi.events import EventEnvelope, EventKind
from orchestratord.spi.backend import SessionSpec
from orchestratord.spi.session import ResumeStatus
from orchestratord.spi.session import ResumeStatus


class HermesSession:
    """Adapts a hermes CLI session into an AgentSession.

    Supports ``--resume`` for session continuity and ``--yolo`` for
    auto-approval mode.  Upgrade path: if hermes gateway protocol opens,
    migrate to Protocol family.
    """

    def __init__(self, spec: SessionSpec) -> None:
        self._spec = spec
        self.session_id = spec.resume_session_id or f"hermes-{id(self)}"
        self.capabilities = BackendCapabilities(
            streaming_deltas=False,
            resumable=True,
            interrupt=False,
            approval_hooks=False,
            parallel_sessions=True,
            cost_reporting=False,
            tool_filtering=False,
            takeover=False,
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

        args = ["hermes", "chat", "--yolo", "--pass-session-id"]
        if self._spec.resume_session_id:
            args.extend(["--resume", self._spec.resume_session_id])
        if self._spec.model:
            args.extend(["--model", self._spec.model])

        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                cwd=self._spec.cwd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            stdout, stderr = await asyncio.wait_for(
                proc.communicate(input=text.encode()),
                timeout=600.0,
            )

            output = stdout.decode("utf-8", errors="replace").strip()
            if output:
                self._events.append(
                    EventEnvelope(
                        seq=self._next_seq(), timestamp=self._now(),
                        kind=EventKind.TEXT, payload={"text": output},
                    )
                )
        except Exception as exc:
            self._events.append(
                EventEnvelope(
                    seq=self._next_seq(), timestamp=self._now(),
                    kind=EventKind.ERROR,
                    payload={"code": "hermes_error", "message": str(exc)},
                )
            )

        self._events.append(
            EventEnvelope(
                seq=self._next_seq(), timestamp=self._now(),
                kind=EventKind.TURN_COMPLETE, payload={"reason": "success"},
            )
        )
        self._events.append(
            EventEnvelope(
                seq=self._next_seq(), timestamp=self._now(),
                kind=EventKind.SESSION_COMPLETE, payload={"reason": "success"},
            )
        )

    async def _emit_events(self):
        for ev in self._events:
            yield ev
        self._events.clear()

    def events(self) -> AsyncIterator[EventEnvelope]:
        return self._emit_events()

    async def interrupt(self) -> None:
        pass

    async def approve(self, request_id: str, decision: ApprovalDecision) -> None:
        pass

    async def probe_resume(self) -> ResumeStatus:
        """Hermes explicitly does not support cross-process resume.

        Per ADR-003 / DESIGN §3.3 the hermes backend must return
        ``REJECTED`` so the orchestrator does not waste a turn on a
        doomed ``send()``. The orchestrator will emit a structured
        ``resume_rejected`` event with ``reason='unsupported'``.
        """
        if not self._spec.resume_session_id:
            return ResumeStatus.RESUMED
        return ResumeStatus.REJECTED

    async def close(self) -> None:
        self._closed = True
