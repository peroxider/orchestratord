"""ClawcodexSession — wraps QueryRunner into the AgentSession SPI.

Supports multi-turn operation: each ``send()`` call starts a new turn
via a fresh ``QueryRunner``, and ``events()`` yields the translated
``EventEnvelope`` stream for that turn.  The session is finished when
a ``SESSION_COMPLETE`` event is emitted or ``close()`` is called.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from orchestratord.spi.approval import ApprovalDecision
from orchestratord.spi.capabilities import BackendCapabilities
from orchestratord.spi.events import EventEnvelope, EventKind
from orchestratord.spi.backend import SessionSpec
from orchestratord.spi.session import ResumeStatus

# Ensure the clawcodex-ascend source tree is on sys.path so the
# ``extensions.api.query`` bridge module is importable.  The backend
# already depends on the clawcodex runtime by contract; this just
# resolves the filesystem location.
_CCX_SOURCE = os.environ.get(
    "CLAWCODEX_SOURCE",
    "/mnt/c/WorkSpace/AgentSDK/clawcodex-ascend",
)
if _CCX_SOURCE not in sys.path:
    sys.path.insert(0, _CCX_SOURCE)

logger = logging.getLogger(__name__)


class ClawcodexSession:
    """Adapts a clawcodex QueryRunner session into an AgentSession.

    Multi-turn architecture:
    - ``send(content)`` starts a new turn (non-blocking — creates a
      background ``asyncio.Task``).
    - ``events()`` yields ``EventEnvelope`` objects from an internal
      queue until the turn's sentinel (``None``) is received.
    - Subsequent ``send()`` calls wait for the previous turn to finish
      before starting a new one.
    """

    def __init__(self, spec: SessionSpec) -> None:
        self._spec = spec
        self.session_id = spec.resume_session_id or f"ccx-{id(self)}"
        self.capabilities = BackendCapabilities(
            streaming_deltas=True,
            resumable=False,
            interrupt=False,
            approval_hooks=True,
            parallel_sessions=False,
            cost_reporting=True,
            tool_filtering=True,
            takeover=True,
            goal_mode=True,
            # resume_detection=True: clawcodex exposes a transcript
            # probe via ``extensions.api.query.QueryRunner.probe_transcript``
            # (DESIGN_graded_timeouts_and_resume.md §3.3). The probe is
            # only meaningful when ``spec.resume_session_id`` is set.
            resume_detection=True,
        )
        self._event_queue: asyncio.Queue[EventEnvelope | None] = asyncio.Queue()
        self._current_task: asyncio.Task[Any] | None = None
        self._seq = 0
        self._closed = False
        # Goal-mode state
        self._goal_mgr: Any = None
        self._goal_workspace: str | None = None
        self._goal_state: dict[str, Any] | None = None
        self._cumulative_tokens: int = 0
        self._cumulative_cost_usd: float = 0.0
        self._events_buffer: list[EventEnvelope] = []

        if spec.goal_condition:
            self._init_goal_manager(spec)

    # ------------------------------------------------------------------
    # SPI: AgentSession
    # ------------------------------------------------------------------

    async def send(self, content: str | list[Any]) -> None:
        """Start a new turn.  Non-blocking — events arrive via ``events()``.

        If a previous turn is still running, waits for it to finish
        before starting the new one.
        """
        if self._closed:
            raise RuntimeError("session closed")

        # Wait for the previous turn to finish, if any.
        if self._current_task is not None and not self._current_task.done():
            logger.debug("ClawcodexSession.send: waiting for previous turn to finish")
            await self._current_task

        text = content if isinstance(content, str) else str(content)
        self._current_task = asyncio.create_task(self._run_turn(text))

    def events(self) -> AsyncIterator[EventEnvelope]:
        """Yield events from the current turn until the sentinel."""
        return self._emit_events()

    async def interrupt(self) -> None:
        """Clawcodex QueryRunner does not support interrupt natively."""
        pass

    async def approve(self, request_id: str, decision: ApprovalDecision) -> None:
        """Approval is handled by the clawcodex tool system natively."""
        pass

    async def probe_resume(self) -> ResumeStatus:
        """Probe whether the resume target transcript is reachable.

        Behavior matrix:
          * No ``resume_session_id`` set → ``RESUMED`` (nothing to
            resume, vacuously OK)
          * ``resume_session_id`` set, QueryRunner not available →
            ``UNDETECTABLE`` (cannot probe without the SDK)
          * SDK probe returns OK → ``RESUMED``
          * SDK probe raises / returns gone → ``REJECTED``

        The probe is delegated to ``extensions.api.query.QueryRunner.probe_transcript``
        when available. The SDK call is wrapped in a short timeout
        (``handshake_timeout_s`` from spec, default 30s) so a hung
        probe does not stall the orchestrator.
        """
        if not self._spec.resume_session_id:
            # No target to probe — fresh session, treat as resumed.
            return ResumeStatus.RESUMED
        try:
            from extensions.api.query import QueryRunner
        except ImportError:
            logger.debug(
                "ClawcodexSession.probe_resume: QueryRunner not on "
                "PYTHONPATH — returning UNDETECTABLE"
            )
            return ResumeStatus.UNDETECTABLE

        try:
            probe_timeout = self._spec.handshake_timeout_s or 30.0
            ok = await asyncio.wait_for(
                asyncio.to_thread(
                    QueryRunner.probe_transcript,
                    self._spec.resume_session_id,
                    workspace=self._spec.cwd,
                ),
                timeout=probe_timeout,
            )
            return ResumeStatus.RESUMED if ok else ResumeStatus.REJECTED
        except asyncio.TimeoutError:
            logger.warning(
                "ClawcodexSession.probe_resume: probe timed out after %.1fs — "
                "returning UNDETECTABLE",
                probe_timeout,
            )
            return ResumeStatus.UNDETECTABLE
        except Exception as exc:
            logger.warning(
                "ClawcodexSession.probe_resume: probe raised %s — returning REJECTED",
                exc,
            )
            return ResumeStatus.REJECTED

    async def close(self) -> None:
        """Cancel any pending turn and mark the session closed."""
        self._closed = True
        # Serialize goal state before teardown
        if self._goal_mgr is not None:
            try:
                self._goal_state = self._goal_mgr.state.to_dict()
            except Exception:
                logger.warning("ClawcodexSession: failed to serialize goal state", exc_info=True)
        if self._current_task is not None and not self._current_task.done():
            self._current_task.cancel()
            try:
                await self._current_task
            except asyncio.CancelledError:
                pass

    def close_sync(self) -> None:
        """Synchronous close — best-effort, does not await task cancellation."""
        self._closed = True
        if self._current_task is not None and not self._current_task.done():
            self._current_task.cancel()

    # ------------------------------------------------------------------
    # Internal: goal mode
    # ------------------------------------------------------------------

    def _init_goal_manager(self, spec: SessionSpec) -> None:
        """Lazy-import and initialize the GoalManager for Ralph-loop mode."""
        try:
            from src.goals import GoalManager, build_judge_callable
        except ImportError:
            logger.warning(
                "ClawcodexSession: goal_condition set but GoalManager not "
                "available (clawcodex src/goals not on PYTHONPATH)"
            )
            return

        judge = build_judge_callable(spec.provider or "deepseek")
        self._goal_mgr = GoalManager(
            session_id=self.session_id,
            default_max_turns=spec.goal_max_turns or 20,
            judge=judge,
        )
        self._goal_mgr.set(
            spec.goal_condition,
            max_turns=spec.goal_max_turns,
            subgoals=spec.goal_subgoals or [],
        )
        self._goal_workspace = spec.cwd
        self._add_event(EventKind.GOAL_SET, {
            "condition": spec.goal_condition[:4000],
            "max_turns": self._goal_mgr.state.max_turns,
            "subgoals": spec.goal_subgoals or [],
        })

    def _add_event(self, kind: EventKind, payload: dict[str, Any]) -> None:
        """Queue an event envelope synchronously (for use outside _run_turn)."""
        envelope = EventEnvelope(
            seq=self._next_seq(),
            timestamp=self._now(),
            kind=kind,
            payload=payload,
        )
        self._event_queue.put_nowait(envelope)

    def _check_workspace_change(self) -> bool:
        """Check if the workspace changed since the goal was set.

        Returns True if the goal was cleared due to workspace change.
        """
        if self._goal_mgr is None or self._goal_workspace is None:
            return False
        if self._spec.cwd != self._goal_workspace:
            old_ws = self._goal_workspace
            new_ws = self._spec.cwd
            logger.warning(
                "ClawcodexSession: workspace changed, clearing goal — "
                "from_workspace=%r to_workspace=%r",
                old_ws,
                new_ws,
            )
            self._goal_mgr.clear()
            self._add_event(EventKind.GOAL_CLEARED, {
                "reason": "workspace-changed",
                "from_workspace": old_ws,
                "to_workspace": new_ws,
            })
            self._goal_mgr = None
            self._goal_workspace = None
            return True
        return False

    async def _evaluate_goal(self) -> None:
        """Run the goal evaluate-continue loop after a turn completes.

        If the judge says "continue", recursively calls _run_turn with
        the continuation prompt. Bounded by goal_max_turns.
        """
        if self._goal_mgr is None or not self._goal_mgr.is_active():
            return

        if self._check_workspace_change():
            return

        evidence = self._collect_turn_evidence()
        try:
            decision = self._goal_mgr.evaluate_after_turn(
                evidence,
                tokens_now=self._cumulative_tokens,
                cost_now_usd=self._cumulative_cost_usd,
            )
        except Exception:
            logger.warning(
                "ClawcodexSession: judge evaluate_after_turn raised; "
                "continuing loop (fail-open)",
                exc_info=True,
            )
            decision = {"verdict": "continue", "should_continue": True,
                        "continuation_prompt": None, "reason": "judge error (fail-open)"}

        self._add_event(EventKind.GOAL_STATUS, {
            "status": decision.get("status", "unknown"),
            "turns_used": self._goal_mgr.state.turns_used,
            "tokens_used": self._goal_mgr.state.spent_tokens,
        })

        if decision.get("should_continue") and decision.get("continuation_prompt"):
            self._add_event(EventKind.GOAL_CONTINUE, {
                "reason": decision.get("reason", ""),
                "continuation_prompt": decision["continuation_prompt"][:4000],
            })
            await self._run_turn(decision["continuation_prompt"])
            return

        verdict = decision.get("verdict", "unknown")
        if verdict == "done":
            self._add_event(EventKind.GOAL_DONE, {
                "reason": decision.get("reason", ""),
            })
        elif verdict in ("timeout", "skipped", "paused"):
            self._add_event(EventKind.GOAL_PAUSED, {
                "reason": decision.get("reason", verdict),
                "resume_hint": "/goal resume",
            })

    def _collect_turn_evidence(self) -> list[dict[str, Any]]:
        """Collect the last 50 event envelopes as judge evidence."""
        recent = self._events_buffer[-50:]
        return [
            {"kind": e.kind.value, "payload": e.payload}
            for e in recent
        ]

    # ------------------------------------------------------------------
    # Internal: turn execution
    # ------------------------------------------------------------------

    async def _run_turn(self, content: str) -> None:
        """Run a single turn via QueryRunner, pushing events to the queue.

        A ``None`` sentinel is always pushed to the queue after the turn
        finishes (success or failure), so ``events()`` never hangs.
        """
        try:
            from extensions.api.query import QueryConfig, QueryRunner

            config = QueryConfig(
                prompt=content,
                workspace=self._spec.cwd,
                provider=self._spec.provider,
                model=self._spec.model,
                max_turns=self._spec.max_turns,
                permission_mode=self._spec.permission_mode,
                append_system_prompt=self._spec.system_prompt,
                tools=self._spec.tools_allow,
                env=self._spec.env,
                resume_session_id=self._spec.resume_session_id,
                run_id=self._spec.run_id,
                debug_log_path=self._spec.debug_log_path,
                timeout_s=self._spec.timeout_s,
                stall_timeout_s=self._spec.stall_timeout_s,
                stall_warn_s=self._spec.stall_warn_s,
                agent_id=self._spec.extra.get("agent_id"),
                runtime_tasks=self._spec.extra.get("runtime_tasks"),
                control_drain_fn=self._spec.extra.get("control_drain_fn"),
                pause_wait_fn=self._spec.extra.get("pause_wait_fn"),
                pause_gate=self._spec.extra.get("pause_gate"),
            )
            runner = QueryRunner(config)
            async for event in runner.stream():
                translated = self._translate_event(event)
                if translated is not None:
                    await self._event_queue.put(translated)
                    self._events_buffer.append(translated)
            # After turn completes, run goal evaluation if active
            await self._evaluate_goal()
        except ImportError:
            await self._event_queue.put(
                EventEnvelope(
                    seq=self._next_seq(),
                    timestamp=self._now(),
                    kind=EventKind.ERROR,
                    payload={"message": "Clawcodex backend: QueryRunner not available"},
                )
            )
        except asyncio.CancelledError:
            # Turn was cancelled via close() — still need to send sentinel.
            raise
        except Exception as exc:
            logger.debug("ClawcodexSession._run_turn error: %s", exc, exc_info=True)
            await self._event_queue.put(
                EventEnvelope(
                    seq=self._next_seq(),
                    timestamp=self._now(),
                    kind=EventKind.ERROR,
                    payload={"message": str(exc)},
                )
            )
        finally:
            # Sentinel: signals end of this turn's event stream.
            await self._event_queue.put(None)

    # ------------------------------------------------------------------
    # Internal: event translation
    # ------------------------------------------------------------------

    def _translate_event(self, event: Any) -> EventEnvelope | None:
        """Translate a clawcodex API event into an EventEnvelope.

        Maps the concrete field names from ``extensions.api.query`` event
        types onto the SPI ``EventEnvelope`` payload keys.  See the
        ``QueryEvent`` type union for the canonical field definitions:
        - TextDelta.content: str
        - ToolCallEvent.tool_name, .params, .tool_use_id
        - ToolResultEvent.tool_name, .result, .tool_use_id
        - TurnComplete.turn: int
        - PhaseComplete.phase: int, .turn_count: int
        - SessionComplete.reason: str
        """
        from extensions.api.query import (
            PhaseComplete,
            SessionComplete,
            TextDelta,
            ToolCallEvent,
            ToolResultEvent,
            TurnComplete,
        )

        if isinstance(event, TextDelta):
            return EventEnvelope(
                seq=self._next_seq(),
                timestamp=self._now(),
                kind=EventKind.TEXT_DELTA,
                payload={"text": event.content, "delta": event.content},
            )
        elif isinstance(event, ToolCallEvent):
            return EventEnvelope(
                seq=self._next_seq(),
                timestamp=self._now(),
                kind=EventKind.TOOL_CALL,
                payload={
                    "call_id": event.tool_use_id,
                    "name": event.tool_name,
                    "arguments": event.params,
                    "approved": getattr(event, "_approved", None),
                    "deny_reason": getattr(event, "_deny_reason", None),
                },
            )
        elif isinstance(event, ToolResultEvent):
            return EventEnvelope(
                seq=self._next_seq(),
                timestamp=self._now(),
                kind=EventKind.TOOL_RESULT,
                payload={
                    "call_id": event.tool_use_id,
                    "ok": not event.result.get("is_error", False),
                    "output": event.result.get("output", ""),
                },
            )
        elif isinstance(event, TurnComplete):
            return EventEnvelope(
                seq=self._next_seq(),
                timestamp=self._now(),
                kind=EventKind.TURN_COMPLETE,
                payload={
                    "reason": str(event.turn),
                    "turn": event.turn,
                },
            )
        elif isinstance(event, PhaseComplete):
            return EventEnvelope(
                seq=self._next_seq(),
                timestamp=self._now(),
                kind=EventKind.PHASE_COMPLETE,
                payload={
                    "phase": str(event.phase),
                    "turn_count": event.turn_count,
                },
            )
        elif isinstance(event, SessionComplete):
            return EventEnvelope(
                seq=self._next_seq(),
                timestamp=self._now(),
                kind=EventKind.SESSION_COMPLETE,
                payload={"reason": event.reason},
            )
        return None

    # ------------------------------------------------------------------
    # Internal: helpers
    # ------------------------------------------------------------------

    async def _emit_events(self) -> AsyncIterator[EventEnvelope]:
        """Yield events from the queue until the sentinel (None)."""
        while True:
            event = await self._event_queue.get()
            if event is None:
                break
            yield event

    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq

    def _now(self) -> float:
        return time.time()