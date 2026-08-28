"""CodexSession — wraps codex exec --json into the AgentSession SPI."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator
from typing import Any

from orchestratord.spi.approval import ApprovalDecision
from orchestratord.spi.capabilities import BackendCapabilities
from orchestratord.spi.events import EventEnvelope, EventKind
from orchestratord.spi.backend import SessionSpec
from orchestratord.spi.session import ResumeStatus

logger = __import__("logging").getLogger(__name__)


class CodexSession:
    """Adapts a codex CLI session into an AgentSession.

    Each ``send()`` spawns ``codex exec --json`` as a subprocess.
    Resumable via ``codex exec resume --last``.
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
            # ADR-003: the codex CLI is a per-turn subprocess wrapper;
            # there is no cross-process state to probe before send.
            # The orchestrator must surface this as UNDETECTABLE.
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

        args = ["codex", "exec", "--json"]
        if self._spec.model:
            args.extend(["-m", self._spec.model])

        # Resume if we have a previous session
        if self._spec.resume_session_id:
            args.append("resume")
            args.append(self._spec.resume_session_id)
        else:
            args.append(text)

        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                cwd=self._spec.cwd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            stdout, stderr = await asyncio.wait_for(
                proc.communicate(input=text.encode() if self._spec.resume_session_id else None),
                timeout=600.0,
            )

            output = stdout.decode("utf-8", errors="replace").strip()
            if output:
                try:
                    data = json.loads(output)
                    text_output = data.get("output", data.get("text", output))
                    if isinstance(text_output, list):
                        text_output = "\n".join(str(b) for b in text_output)
                except json.JSONDecodeError:
                    text_output = output

                self._events.append(
                    EventEnvelope(
                        seq=self._next_seq(),
                        timestamp=self._now(),
                        kind=EventKind.TEXT,
                        payload={"text": str(text_output)},
                    )
                )
        except Exception as exc:
            self._events.append(
                EventEnvelope(
                    seq=self._next_seq(),
                    timestamp=self._now(),
                    kind=EventKind.ERROR,
                    payload={"code": "codex_error", "message": str(exc)},
                )
            )

        self._events.append(
            EventEnvelope(
                seq=self._next_seq(),
                timestamp=self._now(),
                kind=EventKind.TURN_COMPLETE,
                payload={"reason": "success"},
            )
        )
        self._events.append(
            EventEnvelope(
                seq=self._next_seq(),
                timestamp=self._now(),
                kind=EventKind.SESSION_COMPLETE,
                payload={"reason": "success"},
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
        """CLI backend has no cross-process state — always UNDETECTABLE.

        The orchestrator should still attempt ``send()``; if the
        ``codex exec resume`` subprocess fails it will surface as
        an ERROR event via the normal stream.
        """
        if not self._spec.resume_session_id:
            return ResumeStatus.RESUMED
        return ResumeStatus.UNDETECTABLE

    async def close(self) -> None:
        self._closed = True
