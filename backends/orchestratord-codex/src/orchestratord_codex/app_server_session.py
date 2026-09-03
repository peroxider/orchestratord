"""CodexAppServerSession — Protocol backend via codex app-server JSON-RPC over stdio.

Uses the orchestratord bridge protocol module for worker lifecycle management.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections.abc import AsyncIterator
from typing import Any

from orchestratord.bridge.worker import WorkerManager
from orchestratord.spi.approval import ApprovalDecision
from orchestratord.spi.backend import SessionSpec
from orchestratord.spi.capabilities import BackendCapabilities
from orchestratord.spi.events import EventEnvelope, EventKind
from orchestratord.spi.session import ResumeStatus

logger = logging.getLogger(__name__)


class CodexAppServerSession:
    """Adapts codex app-server (JSON-RPC over stdio) into AgentSession.

    Uses ``codex app-server --listen stdio://`` as the worker process.
    """

    def __init__(self, spec: SessionSpec) -> None:
        self._spec = spec
        self.session_id = spec.resume_session_id or f"codex-as-{id(self)}"
        self.capabilities = BackendCapabilities(
            streaming_deltas=True,
            resumable=False,
            interrupt=True,
            approval_hooks=True,
            parallel_sessions=True,
            cost_reporting=False,
            tool_filtering=False,
            takeover=False,
            # codex app-server exposes ``session/load`` MCP
            # call to check whether a session is still alive on the
            # server side. Resume probes translate the response into
            # the three-state ResumeStatus.
            resume_detection=True,
        )
        self._events: list[EventEnvelope] = []
        self._seq = 0
        self._closed = False
        self._worker: WorkerManager | None = None

    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq

    def _now(self) -> float:
        return time.time()

    async def _ensure_worker(self) -> WorkerManager:
        if self._worker is not None:
            return self._worker

        args = ["codex", "app-server", "--listen", "stdio://"]
        if self._spec.model:
            args.extend(["-c", f"model={self._spec.model}"])

        child_env = dict(os.environ)
        child_env.update(self._spec.env)
        worker = WorkerManager(
            worker_cmd=args,
            cwd=self._spec.cwd,
            env=child_env,
        )
        await worker.start()
        self._worker = worker
        return worker

    async def send(self, content: str | list[Any]) -> None:
        if self._closed:
            raise RuntimeError("session closed")

        try:
            worker = await self._ensure_worker()
            text = content if isinstance(content, str) else str(content)

            response = await worker.session_prompt(self.session_id, text)

            if response.get("error"):
                err = response["error"]
                self._events.append(
                    EventEnvelope(
                        seq=self._next_seq(), timestamp=self._now(),
                        kind=EventKind.ERROR,
                        payload={"code": str(err.get("code", "")), "message": err.get("message", "")},
                    )
                )
            else:
                result = response.get("result", {})
                output_text = result.get("output", result.get("text", str(result)))
                self._events.append(
                    EventEnvelope(
                        seq=self._next_seq(), timestamp=self._now(),
                        kind=EventKind.TEXT,
                        payload={"text": str(output_text)},
                    )
                )
        except Exception as exc:
            self._events.append(
                EventEnvelope(
                    seq=self._next_seq(), timestamp=self._now(),
                    kind=EventKind.ERROR,
                    payload={"code": "codex_as_error", "message": str(exc)},
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
        if self._worker is not None:
            await self._worker.session_cancel(self.session_id)

    async def approve(self, request_id: str, decision: ApprovalDecision) -> None:
        pass

    async def probe_resume(self) -> ResumeStatus:
        """Probe whether the resume target transcript is reachable.

        Strategy: POST a JSON-RPC ``session/load`` request via the
        codex app-server worker and translate the response code.

        Returns:
            RESUMED       — worker reports the session is reachable
            REJECTED      — worker reports 404 / session-not-found
            UNDETECTABLE  — worker not started yet, or RPC failure
        """
        if not self._spec.resume_session_id:
            return ResumeStatus.RESUMED
        if self._worker is None:
            # Worker hasn't been started yet; the protocol does not
            # expose a probe path before first send. Be honest about it.
            return ResumeStatus.UNDETECTABLE
        try:
            probe_timeout = self._spec.handshake_timeout_s or 30.0
            response = await asyncio.wait_for(
                self._worker.session_load(self._spec.resume_session_id),
                timeout=probe_timeout,
            )
            if response.get("error"):
                return ResumeStatus.REJECTED
            result = response.get("result", {})
            return (
                ResumeStatus.RESUMED
                if result.get("exists", False)
                else ResumeStatus.REJECTED
            )
        except asyncio.TimeoutError:
            logger.warning(
                "CodexAppServerSession.probe_resume: RPC timed out after %.1fs",
                probe_timeout,
            )
            return ResumeStatus.UNDETECTABLE
        except Exception as exc:
            logger.warning(
                "CodexAppServerSession.probe_resume: RPC failed: %s",
                exc,
            )
            return ResumeStatus.UNDETECTABLE

    async def close(self) -> None:
        if self._worker is not None:
            await self._worker.stop()
        self._closed = True

    def close_sync(self) -> None:
        self._closed = True
