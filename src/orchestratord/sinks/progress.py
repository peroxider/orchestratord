"""ProgressSink protocol and concrete sinks.

This module replaces the legacy-era single-instance :class:`ProgressReporter`
with a per-session :class:`ProgressSink` protocol so that the orchestrator
can fan out agent progress events to multiple consumers without sharing
mutable state between concurrent issues.

Design references:

* :class:`ProgressSink` — minimal protocol; consumers implement three
  ``on_*_complete`` methods and own their private state via the bound
  ``task_id``.
* :class:`CompositeProgressSink` — synchronous fan-out with per-sink
  exception isolation; one bad consumer never blocks the others.
* :class:`ToolContextProgressSink` — the default implementation that
  writes events to the log and event bus (no longer depends on
  backend ToolContext). Uses :attr:`workflow_phases` to compute
  honest progress percentages when available, and falls back to
  ``None`` (UI shows "unknown") when no phase weights are configured.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Iterable, Protocol, runtime_checkable

from orchestratord.events.agent_events import PhaseComplete, SessionComplete, TurnComplete

if TYPE_CHECKING:
    from ..session_state import AgentSession

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class ProgressSink(Protocol):
    """A consumer of agent progress events for ONE task / session.

    Each :class:`ProgressSink` instance is bound to a single ``task_id``
    and its own private state (phase counter, configured phase list, …).
    Because the instance is never accessed by more than one
    :class:`AgentSession`, the protocol makes no threading guarantees —
    internal counters can be plain ints.

    The three ``on_*_complete`` methods are dispatched by the agent
    runner at well defined points in the session lifecycle:

    * :meth:`on_phase_complete` — a logical phase (one or more turns)
      finished. ``event.phase`` is the 1-based phase number.
    * :meth:`on_turn_complete` — a single turn finished; ``event.turn``
      is the 1-based turn counter.
    * :meth:`on_session_complete` — the whole session is ending.
      ``event.reason`` is one of ``"success"``, ``"stagnation"``,
      ``"loop_detected"``, ``"noop_completed"``, ``"budget_exhausted"``,
      ``"max_turns_exceeded"``, ``"rate_limit_circuit_open"``,
      ``"exit_code=N"`` (runner-level termination reason).
    """

    task_id: str

    def on_phase_complete(
        self,
        event: PhaseComplete,
        session: "AgentSession",
    ) -> None: ...

    def on_turn_complete(
        self,
        event: TurnComplete,
        session: "AgentSession",
    ) -> None: ...

    def on_session_complete(
        self,
        event: SessionComplete,
        session: "AgentSession",
    ) -> None: ...


# ---------------------------------------------------------------------------
# Composite (fan-out with exception isolation)
# ---------------------------------------------------------------------------


class CompositeProgressSink:
    """Synchronous fan-out over a list of :class:`ProgressSink` consumers.

    Each child sink runs sequentially in registration order; an exception
    raised by one sink is logged via :func:`logger.exception` and the
    remaining sinks still receive the event. The composite never raises
    out of its ``on_*_complete`` methods.

    Sinks are mutable: :meth:`add` lets the orchestrator register
    additional consumers (e.g. the CI auto-fix sink :class:`PRReviewAutoFixSink` or
    the retry label sink :class:`RetryLabelSink`) without touching the runner.
    """

    def __init__(self, sinks: Iterable[ProgressSink] = ()) -> None:
        self._sinks: list[ProgressSink] = list(sinks)

    # The composite itself satisfies the ProgressSink protocol — its
    # ``task_id`` is empty because it is not bound to a single task.
    task_id: str = ""

    def add(self, sink: ProgressSink) -> None:
        """Append ``sink`` to the fan-out list."""
        self._sinks.append(sink)

    def __len__(self) -> int:
        return len(self._sinks)

    def __iter__(self):
        return iter(self._sinks)

    def _dispatch(
        self,
        method_name: str,
        event: Any,
        session: "AgentSession",
    ) -> None:
        for sink in list(self._sinks):
            method = getattr(sink, method_name, None)
            if method is None:
                logger.debug(
                    "sink %s has no %s method, skipping",
                    sink,
                    method_name,
                )
                continue
            try:
                method(event, session)
            except Exception as exc:  # noqa: BLE001
                logger.exception(
                    "progress_sink.%s dispatch failed for sink=%r: %s",
                    method_name,
                    sink,
                    exc,
                )

    def on_phase_complete(
        self,
        event: PhaseComplete,
        session: "AgentSession",
    ) -> None:
        self._dispatch("on_phase_complete", event, session)

    def on_turn_complete(
        self,
        event: TurnComplete,
        session: "AgentSession",
    ) -> None:
        self._dispatch("on_turn_complete", event, session)

    def on_session_complete(
        self,
        event: SessionComplete,
        session: "AgentSession",
    ) -> None:
        self._dispatch("on_session_complete", event, session)


# ---------------------------------------------------------------------------
# Default ToolContext-backed implementation
# ---------------------------------------------------------------------------


class ToolContextProgressSink:
    """Default :class:`ProgressSink` that logs progress events.

    Writes progress events to the log and, when a legacy context is supplied,
    mirrors them into ``context.tasks[task_id].metadata.progress_stages``.
    The mirror is duck-typed: core does not import a backend ToolContext.

    Progress percentage policy:

    * When ``workflow_phases`` is configured, the nth phase receives
      ``(n / total) * 100`` so users see a meaningful number that
      tracks real workflow progress.
    * When ``fallback_to_phase_step`` is True, fall back to
      ``min(idx * 25, 100)`` (the old legacy behavior) for soft migration.
    * Otherwise the sink writes ``None`` so the dashboard displays
      "Phase N (progress unknown)" instead of the misleading
      ``25 / 50 / 75 / 100`` sequence.

    :class:`SessionComplete` always emits a single stage named
    ``session_{reason}`` and only fakes ``progress=100`` for the
    ``"success"`` reason — other reasons (``stagnation``,
    ``loop_detected``, ``noop_completed``, ``budget_exhausted``, …) get
    ``progress=None`` so the dashboard never lies about a failed run.
    """

    def __init__(
        self,
        task_id: str,
        workflow_phases: list[str] | None = None,
        fallback_to_phase_step: bool = False,
        # Optional compatibility surface for dashboards that still consume
        # ToolContext-shaped task metadata.  It remains duck-typed so the
        # core does not acquire a backend import.
        context: Any = None,
    ) -> None:
        self.task_id = task_id
        self._phase_count = 0
        self._workflow_phases: list[str] = list(workflow_phases or [])
        self._fallback_to_phase_step = fallback_to_phase_step
        self._legacy_context = context

    # -- helpers ---------------------------------------------------------

    def _named_phase(self, idx: int) -> str:
        if 1 <= idx <= len(self._workflow_phases):
            return self._workflow_phases[idx - 1]
        return f"phase_{idx}"

    def _phase_progress(self, idx: int) -> int | None:
        """Return the progress percentage for the nth phase, or None."""
        if self._workflow_phases:
            try:
                real_idx = self._workflow_phases.index(self._named_phase(idx))
            except ValueError:
                return 100
            return int((real_idx + 1) / len(self._workflow_phases) * 100)
        if self._fallback_to_phase_step:
            return min(idx * 25, 100)
        return None

    def _record_stage(self, stage: str, progress: int | None) -> None:
        """Best-effort compatibility write into a ToolContext-shaped object."""
        if self._legacy_context is None or not self.task_id:
            return
        tasks = getattr(self._legacy_context, "tasks", None)
        if not isinstance(tasks, dict):
            return
        task = tasks.setdefault(self.task_id, {"id": self.task_id, "metadata": {}})
        if not isinstance(task, dict):
            return
        metadata = task.setdefault("metadata", {})
        if not isinstance(metadata, dict):
            return
        row: dict[str, Any] = {"stage": stage}
        if progress is not None:
            row["progress"] = progress
        metadata.setdefault("progress_stages", []).append(row)

    # -- dispatch --------------------------------------------------------

    def on_phase_complete(
        self,
        event: PhaseComplete,
        session: "AgentSession",
    ) -> None:
        if not self.task_id:
            return
        self._phase_count += 1
        phase_idx = event.phase or self._phase_count
        phase_name = self._named_phase(phase_idx)
        progress = self._phase_progress(phase_idx)
        self._record_stage(phase_name, progress)
        logger.info(
            "progress: task=%s phase=%d/%s progress=%s turn_count=%d",
            self.task_id,
            phase_idx,
            phase_name,
            f"{progress}%" if progress is not None else "unknown",
            event.turn_count,
        )

    def on_turn_complete(
        self,
        event: TurnComplete,
        session: "AgentSession",
    ) -> None:
        logger.debug(
            "turn %d complete for task %s (issue=%s)",
            event.turn,
            self.task_id,
            getattr(getattr(session, "issue", None), "identifier", "unknown"),
        )

    def on_session_complete(
        self,
        event: SessionComplete,
        session: "AgentSession",
    ) -> None:
        if not self.task_id:
            return
        progress = 100 if event.reason == "success" else None
        self._record_stage(f"session_{event.reason}", progress)
        logger.info(
            "session complete: task=%s reason=%s progress=%s turn_count=%d phase_count=%d",
            self.task_id,
            event.reason,
            f"{progress}%" if progress is not None else "unknown",
            getattr(session, "turn_count", 0),
            self._phase_count,
        )
