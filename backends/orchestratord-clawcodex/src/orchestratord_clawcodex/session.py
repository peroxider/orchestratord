"""ClawcodexSession — wraps QueryRunner into the AgentSession SPI.

Supports multi-turn operation: each ``send()`` call starts a new turn
via a fresh ``QueryRunner``, and ``events()`` yields the translated
``EventEnvelope`` stream for that turn.  The session is finished when
a ``SESSION_COMPLETE`` event is emitted or ``close()`` is called.
"""

from __future__ import annotations

import asyncio
import inspect
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
        self._native_session_id = spec.resume_session_id
        self._structured_output = False
        self._reported_query_turns = 0
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
        self._current_runner: Any | None = None
        self._seq = 0
        self._closed = False
        # Goal-mode state
        self._goal_mgr: Any = None
        self._goal_workspace: str | None = None
        self._goal_state: dict[str, Any] | None = None
        self._cumulative_tokens: int = 0
        self._cumulative_cost_usd: float = 0.0
        self._native_turn_count = 0
        self._turn_open = False  # True while a model output batch is in flight
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
        self._turn_open = False
        self._reported_query_turns = 0
        self._current_task = asyncio.create_task(self._run_turn(text))

    def events(self) -> AsyncIterator[EventEnvelope]:
        """Yield events from the current turn until the sentinel."""
        return self._emit_events()

    async def interrupt(self) -> None:
        """Clawcodex QueryRunner does not support interrupt natively."""

    async def approve(self, request_id: str, decision: ApprovalDecision) -> None:
        """Release a native ClawCodex permission waiter with ``decision``."""
        runner = self._current_runner
        if runner is None or not runner.approve(request_id, decision):
            logger.warning("approve() called for unknown request_id=%s", request_id)

    async def probe_resume(self) -> ResumeStatus:
        """Probe whether the resume target transcript is reachable.

        Behavior matrix:
          * ``resume_session_id`` empty or whitespace-only → ``RESUMED``
            (nothing meaningful to resume, vacuously OK)
          * SDK path: ``extensions.api.query.QueryRunner.probe_transcript``
            exists and returns True/False → ``RESUMED`` /
            ``REJECTED``. SDK import failure, missing attribute, or
            probe exception → fall through to the bypass.
          * Bypass path: ``clawcodex_ext.services.session_storage.resolve_sessions_dir``
            exists and the directory ``<sessions_dir>/<session_id>`` is
            present → ``RESUMED``; otherwise ``REJECTED``. Same probe
            used by the CLI's ``--resume`` flag
            (``clawcodex_ext/cli/dispatch.py``).
          * Both paths unavailable → ``UNDETECTABLE``
          * Either path times out (after ``handshake_timeout_s``,
            default 30s) → ``UNDETECTABLE``

        Both probes run synchronously on a thread via ``asyncio.to_thread``
        so a hung filesystem call cannot stall the event loop.
        """
        if not (self._spec.resume_session_id or "").strip():
            # No usable target — empty or whitespace-only is treated as
            # "nothing to resume" and short-circuits to RESUMED. This
            # also matches the clawcodex ``probe_transcript`` contract
            # (whitespace-only session_id returns False) by keeping the
            # two surfaces consistent: a blank target is never rejected.
            return ResumeStatus.RESUMED

        probe_timeout = self._spec.handshake_timeout_s or 30.0
        session_id = self._spec.resume_session_id

        # --- Path 1: SDK probe (preferred) ---
        sdk_status = await self._probe_resume_via_sdk(
            session_id, probe_timeout
        )
        if sdk_status is not None:
            return sdk_status

        # --- Path 2: bypass via session_storage (CLI --resume parity) ---
        return await self._probe_resume_via_storage(
            session_id, probe_timeout
        )

    async def _probe_resume_via_sdk(
        self, session_id: str, probe_timeout: float
    ) -> ResumeStatus | None:
        """Return the SDK probe outcome, or ``None`` to fall through to bypass.

        ``None`` is returned when the SDK module is missing, the
        ``probe_transcript`` attribute is absent (older clawcodex
        versions), or the probe raised — in all of these cases the
        bypass path may still resolve the question.
        """
        try:
            from extensions.api.query import QueryRunner
        except ImportError:
            logger.debug(
                "ClawcodexSession._probe_resume_via_sdk: QueryRunner not on "
                "PYTHONPATH — falling back to directory check"
            )
            return None
        sdk_probe = getattr(QueryRunner, "probe_transcript", None)
        if not callable(sdk_probe):
            logger.debug(
                "ClawcodexSession._probe_resume_via_sdk: "
                "QueryRunner.probe_transcript missing — falling back to "
                "directory check"
            )
            return None
        try:
            ok = await asyncio.wait_for(
                asyncio.to_thread(
                    sdk_probe,
                    session_id,
                    workspace=self._spec.cwd,
                ),
                timeout=probe_timeout,
            )
            return ResumeStatus.RESUMED if ok else ResumeStatus.REJECTED
        except TimeoutError:
            logger.warning(
                "ClawcodexSession._probe_resume_via_sdk: probe timed out "
                "after %.1fs — falling back to directory check",
                probe_timeout,
            )
            return None
        except Exception as exc:
            logger.warning(
                "ClawcodexSession._probe_resume_via_sdk: probe raised %s — "
                "falling back to directory check",
                exc,
            )
            return None

    async def _probe_resume_via_storage(
        self, session_id: str, probe_timeout: float
    ) -> ResumeStatus:
        """Bypass: replicate the CLI ``--resume`` directory check.

        Mirrors ``clawcodex_ext/cli/dispatch.py``: the session is
        reachable iff ``resolve_sessions_dir() / session_id`` is a
        directory. Workspace isolation is advisory here — clawcodex
        session storage is global (overridable via ``CLAWCODEX_SESSIONS_DIR``).
        """
        try:
            from clawcodex_ext.services.session_storage import (
                resolve_sessions_dir,
            )
        except ImportError:
            logger.debug(
                "ClawcodexSession._probe_resume_via_storage: "
                "session_storage not on PYTHONPATH — returning UNDETECTABLE"
            )
            return ResumeStatus.UNDETECTABLE

        def _check_dir() -> bool:
            try:
                return (resolve_sessions_dir() / session_id).is_dir()
            except Exception:
                # resolve_sessions_dir may read CLAWCODEX_SESSIONS_DIR;
                # surface IOError etc. as "not present" rather than
                # raising into the caller.
                return False

        try:
            ok = await asyncio.wait_for(
                asyncio.to_thread(_check_dir),
                timeout=probe_timeout,
            )
            return ResumeStatus.RESUMED if ok else ResumeStatus.REJECTED
        except asyncio.TimeoutError:
            logger.warning(
                "ClawcodexSession._probe_resume_via_storage: directory "
                "probe timed out after %.1fs — returning UNDETECTABLE",
                probe_timeout,
            )
            return ResumeStatus.UNDETECTABLE
        except AttributeError as exc:
            # QueryRunner.probe_transcript is not yet implemented on the
            # clawcodex side (upstream has no such method). Treat it as
            # undetectable (fresh session, continue) instead of REJECTED,
            # which would fail every run. Aligns with claude/dsh/codex.
            logger.warning(
                "ClawcodexSession.probe_resume: probe interface missing (%s) — "
                "returning UNDETECTABLE",
                exc,
            )
            return ResumeStatus.UNDETECTABLE
        except Exception as exc:
            logger.warning(
                "ClawcodexSession._probe_resume_via_storage: directory "
                "probe raised %s — returning REJECTED",
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
            if self._current_runner is not None:
                self._current_runner.cancel_pending_approvals("session closed")
            self._current_task.cancel()
            try:
                await self._current_task
            except asyncio.CancelledError:
                pass

    def close_sync(self) -> None:
        """Synchronous close — best-effort, does not await task cancellation."""
        self._closed = True
        if self._current_task is not None and not self._current_task.done():
            if self._current_runner is not None:
                self._current_runner.cancel_pending_approvals("session closed")
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
        self._reported_query_turns = 0
        try:
            from extensions.api.query import (
                QueryConfig,
                QueryRunner,
                TextDelta,
                ToolCallEvent,
                ToolResultEvent,
            )

            # Honor the workflow's per-turn timeout (sandbox.turn_timeout_ms)
            # forwarded via spec.extra: clawcodex freeze settings read
            # CLAWCODEX_TURN_TIMEOUT, so set it before the runner starts.
            _tt_ms = self._spec.extra.get("turn_timeout_ms")
            if _tt_ms:
                os.environ["CLAWCODEX_TURN_TIMEOUT"] = str(int(_tt_ms) / 1000)

            config_kwargs: dict[str, Any] = {
                "prompt": content,
                "workspace": self._spec.cwd,
                "provider": self._spec.provider,
                "model": self._spec.model,
                "max_turns": self._spec.max_turns,
                "permission_mode": self._spec.permission_mode,
                "append_system_prompt": self._spec.system_prompt,
                "tools": self._spec.tools_allow,
                "disallowed_tools": self._spec.tools_deny or None,
                "env": self._spec.env,
                # resume_session_id is only populated for genuine resume
                # requests (the core keeps run_id and resume_session_id
                # separate); passing the orchestrator run_id here would make
                # headless Session.resume() fail. None falls through to a
                # fresh session, matching upstream's TEMP-DISABLED guard.
                "resume_session_id": self._native_session_id,
                "run_id": self._spec.run_id,
                "debug_log_path": self._spec.debug_log_path,
                "agent_id": self._spec.extra.get("agent_id"),
                "runtime_tasks": self._spec.extra.get("runtime_tasks"),
                "control_drain_fn": self._spec.extra.get("control_drain_fn"),
                "pause_wait_fn": self._spec.extra.get("pause_wait_fn"),
                "pause_gate": self._spec.extra.get("pause_gate"),
            }
            # SessionSpec timeout_* fields default to None; passing None into
            # QueryConfig makes the stream compare `None > 0` and crash
            # ("'>' not supported between instances of 'NoneType' and
            # 'int'"). Only pass timeouts that are explicitly set, so
            # QueryConfig keeps its own defaults otherwise.
            total_timeout = self._spec.total_timeout_s
            if total_timeout is None:
                total_timeout = self._spec.timeout_s
            if total_timeout is not None:
                config_kwargs["timeout_s"] = total_timeout
            inactivity_timeout = self._spec.inactivity_timeout_s
            if inactivity_timeout is None:
                inactivity_timeout = self._spec.stall_timeout_s
            if inactivity_timeout is not None:
                config_kwargs["stall_timeout_s"] = inactivity_timeout
            if self._spec.stall_warn_s is not None:
                config_kwargs["stall_warn_s"] = self._spec.stall_warn_s
            if self._spec.approval is not None:
                config_kwargs["approval_timeout_s"] = self._spec.approval.timeout_seconds

            # AgentSDK and standalone releases expose different optional
            # QueryConfig fields. Never silently discard a requested feature.
            parameters = inspect.signature(QueryConfig).parameters
            if "approval_hooks" in parameters:
                config_kwargs["approval_hooks"] = True
            self._structured_output = "structured_output" in parameters
            if self._structured_output:
                config_kwargs["structured_output"] = True
            accepts_kwargs = any(
                parameter.kind is inspect.Parameter.VAR_KEYWORD
                for parameter in parameters.values()
            )
            if not accepts_kwargs:
                for name in list(config_kwargs):
                    if name not in parameters:
                        if config_kwargs[name] is not None:
                            raise RuntimeError(
                                f"ClawCodex runtime does not support QueryConfig.{name}"
                            )
                        config_kwargs.pop(name)
                if config_kwargs.get("max_turns") is None:
                    config_kwargs.pop("max_turns", None)

            config = QueryConfig(**config_kwargs)
            runner = QueryRunner(config)
            self._current_runner = runner
            async for event in runner.stream():
                # Turn accounting: clawcodex never emits TurnComplete/
                # PhaseComplete events, so derive turns from the stream —
                # a new model output batch (TextDelta/ToolCallEvent) after a
                # tool result starts a new turn.
                if not self._structured_output and isinstance(event, (TextDelta, ToolCallEvent)) and not self._turn_open:
                    self._native_turn_count += 1
                    self._turn_open = True
                elif isinstance(event, ToolResultEvent):
                    self._turn_open = False
                translated = self._translate_event(event)
                if translated is not None:
                    await self._event_queue.put(translated)
                    self._events_buffer.append(translated)
                # Emit TURN_COMPLETE after each turn's tool results so the
                # orchestrator's turn_count accumulates (Run Summary, stats).
                if not self._structured_output and isinstance(event, ToolResultEvent) and self._native_turn_count:
                    await self._event_queue.put(
                        EventEnvelope(
                            seq=self._next_seq(),
                            timestamp=self._now(),
                            kind=EventKind.TURN_COMPLETE,
                            payload={
                                "turn": self._native_turn_count,
                                "turn_delta": 1,
                            },
                        )
                    )
            # After turn completes, run goal evaluation if active
            await self._evaluate_goal()
        except ImportError as exc:
            # Record the full traceback — the ERROR event below only carries
            # the str() message, which hides WHICH module failed to import
            # (e.g. "QueryRunner not available" gave no clue). With the
            # traceback in the logs, the failing import can be pinpointed.
            logger.exception(
                "ClawcodexSession._run_turn: import failed during turn "
                "(%s) — emitting QueryRunner-not-available",
                exc,
            )
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
            self._current_runner = None
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
        import extensions.api.query as _query_mod
        from extensions.api.query import (
            PhaseComplete,
            SessionComplete,
            TextDelta,
            ToolCallEvent,
            ToolResultEvent,
            TurnComplete,
        )
        # ApprovalRequestEvent is a newer clawcodex interface; the local
        # clawcodex checkout may not expose it yet (version skew, same as
        # probe_transcript). Degrade: when missing, the approval branch
        # simply never matches and no approval event is emitted.
        ApprovalRequestEvent = getattr(_query_mod, "ApprovalRequestEvent", None)
        SessionStarted = getattr(_query_mod, "SessionStarted", None)

        if SessionStarted is not None and isinstance(event, SessionStarted):
            self._native_session_id = event.session_id
            self.session_id = event.session_id
            return EventEnvelope(
                seq=self._next_seq(), timestamp=self._now(), kind=EventKind.SESSION_STARTED,
                payload={"session_id": event.session_id, "model": event.model, "provider": event.provider},
            )
        elif isinstance(event, TextDelta):
            return EventEnvelope(
                seq=self._next_seq(),
                timestamp=self._now(),
                kind=EventKind.TEXT_DELTA,
                payload={"text": event.content, "delta": event.content},
            )
        elif (
            ApprovalRequestEvent is not None
            and isinstance(event, ApprovalRequestEvent)
        ):
            return EventEnvelope(
                seq=self._next_seq(),
                timestamp=self._now(),
                kind=EventKind.APPROVAL_REQUEST,
                payload={
                    "request_id": event.request_id,
                    "call_id": event.call_id,
                    "tool_name": event.tool_name,
                    "arguments": event.arguments,
                    "message": event.message,
                },
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
                    "output": event.result.get("output") if event.result.get("output") is not None else event.result.get("error", ""),
                    "error": event.result.get("error"),
                },
            )
        elif isinstance(event, TurnComplete):
            turn_delta = max(int(event.turn or 0), 1)
            self._native_turn_count += turn_delta
            self._reported_query_turns += turn_delta
            return EventEnvelope(
                seq=self._next_seq(),
                timestamp=self._now(),
                kind=EventKind.TURN_COMPLETE,
                payload={
                    "reason": "completed",
                    "turn": self._native_turn_count,
                    "turn_delta": turn_delta,
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
            native_id = getattr(event, "session_id", None)
            if native_id:
                self._native_session_id = native_id
                self.session_id = native_id
            count = getattr(event, "num_turns", None)
            missing_turns = max(0, count - self._reported_query_turns) if isinstance(count, int) else 0
            if missing_turns:
                self._native_turn_count += missing_turns
                self._add_event(EventKind.TURN_COMPLETE, {
                    "turn": self._native_turn_count, "turn_delta": missing_turns,
                })
            payload: dict[str, Any] = {"reason": event.reason}
            if self._native_session_id:
                payload["session_id"] = self._native_session_id
            usage = getattr(event, "usage", None)
            if isinstance(usage, dict):
                payload["usage"] = dict(usage)
                self._cumulative_tokens += sum(
                    int(usage.get(key) or 0) for key in ("input_tokens", "output_tokens")
                )
            error = getattr(event, "error", None)
            if error:
                payload["error"] = error
                self._add_event(EventKind.ERROR, {"message": error})
            return EventEnvelope(
                seq=self._next_seq(),
                timestamp=self._now(),
                kind=EventKind.SESSION_COMPLETE,
                payload=payload,
            )
        # Keep forward-compatible SDK events visible to the common
        # transcript/event stream instead of silently discarding them.
        raw = (
            dict(event)
            if isinstance(event, dict)
            else vars(event)
            if hasattr(event, "__dict__")
            else {"value": str(event)}
        )
        return EventEnvelope(
            seq=self._next_seq(),
            timestamp=self._now(),
            kind=EventKind.UNKNOWN,
            payload={"event": type(event).__name__, "raw": raw},
        )

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
