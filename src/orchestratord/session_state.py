"""Session state dataclasses — orchestrator runtime state.

These are pure data structures with no dependency on any backend package.
They are consumed by the orchestrator and the generic backend runner.

.. note::

   Session persistence is owned by the orchestrator/backend boundary; this
   module intentionally contains only backend-neutral state.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from .issue_registry.issue import Issue
from .issue_registry.cache import IssueStateCache
from .workspace import Workspace

if TYPE_CHECKING:
    from .agent.task import AgentTask


@dataclass
class AgentSession:
    """One active issue run."""

    issue: Issue  # DEPRECATED: use task instead
    workspace: Workspace
    task: "AgentTask | None" = None  # NEW: generic task abstraction
    turn_count: int = 0
    status: str = "running"  # running, completed, failed
    output_text: str = ""
    # Lifecycle control
    paused: bool = False
    paused_at: float | None = None
    pause_reason: str = ""
    pause_resume_event: "asyncio.Event | None" = None
    # Event stream for CLI tail command
    event_queue: "asyncio.Queue | None" = None
    prompt_override: str | None = None
    # Resolved pre-dispatch clarification context copied from the
    # persistent IssueRecord before the run starts.
    clarification_question: str | None = None
    clarification_answer: str | None = None
    clarification_source: str | None = None
    coordinator_mode: bool | None = None
    # Local Unix-domain socket or loopback TCP listener for live operator control. None if
    # the socket failed to start (or was disabled by configuration). When
    # set, the runner broadcasts every dispatched event and polls for
    # control commands at turn boundaries. Defensive: all socket ops
    # are wrapped in try/except so a broken socket never kills the
    # agent run.
    control_socket: Any | None = None
    # Public Unix path or ``tcp://127.0.0.1:PORT`` endpoint. Stored on the session so the
    # CLI control commands (pause/resume/stop/inject/takeover) can
    # discover it via the registry without scanning the workspace tree.
    control_socket_path: str | None = None
    run_kind: str = "issue"
    run_id: str | None = None
    # Reserved for backend-specific runtime bookkeeping. Core orchestration
    # does not import or depend on a backend task registry.
    _runtime_tasks: Any | None = None
    summary_comment_id: str | None = None
    tool_count: int = 0
    verification_status: str | None = None
    verification_output: str | None = None
    report_path: str | None = None
    # Per-session cache for the tracker poll in ``_should_continue``.
    # Initialised by ``AgentRunner.run()`` from
    # ``agent_config.perf_should_continue_skip_turns``. When ``None`` the
    # runner falls back to the pre-cache behaviour of always polling.
    state_cache: "IssueStateCache | None" = None
    # List of files git left in conflict state. Populated by
    # ``Orchestrator._prepare_rebase_session`` from
    # ``IssueRecord.conflict_files`` when ``run_kind == "agent_rebase"``.
    conflict_files: tuple[str, ...] | None = None
    # Canonical path to ~/.orchestratord/tool-events/{run_id}/events.ndjson.
    tool_events_path: str | None = None
    # Session-transcript storage for conversation recording.
    _transcript_storage: Any | None = None
    _transcript_asst_text: str = ""
    _transcript_tool_uses: list[Any] = field(default_factory=list)
    _transcript_pending_results: dict[str, Any] = field(default_factory=dict)
    _transcript_result_order: list[str] = field(default_factory=list)
    attempt: int = 1
    issue_attempt: int = 1
    followup_attempt: int = 1
    # 429-aware backoff bookkeeping.
    consecutive_429_count: int = 0
    total_429_backoff_seconds: float = 0.0
    rate_limit_pending_turn: int | None = None
    debug_log_path: str | None = None
    last_agent_event_at: float | None = None
    last_agent_event: str | None = None
    last_tool_name: str | None = None
    timeout_deadline_at: float | None = None
    session_end_reason: str | None = None
    session_end_summary: str = ""
    previous_run_ids: list[str] = field(default_factory=list)
    # Per-session followup messages queued by the operator via the
    # chat gateway or control socket.  Appended to the next turn's
    # prompt and cleared.  Backend-agnostic: works for all backends
    # because the runner rebuilds the prompt each turn.
    _pending_followups: list[str] = field(default_factory=list)
    _snapshot_provider: str = ""
    _snapshot_model: str = ""
    _pause_gate: Any = None
    _on_pause_state_change: Any | None = None

    # Kept as an optional compatibility hook for callers that provide their
    # own snapshot implementation. It has no backend-specific default.
    _save_json_snapshot: Any = field(default=None, init=False, repr=False, compare=False)

    # Goal-mode state persisted alongside events for crash recovery.

    # Goal-mode state persisted alongside events for crash recovery.
    # Serialized from GoalManager.state.to_dict() on session close;
    # restored via GoalManager.restore() on resume.
    goal_state: dict | None = None


@dataclass
class RetryItem:
    """Item queued for retry."""

    issue_id: str
    attempt: int
    delay_seconds: float
    identifier: str = ""
    error: str = ""
    worker_host: str | None = None
    workspace_path: str = ""
    scheduled_at: float = field(default_factory=time.time)
