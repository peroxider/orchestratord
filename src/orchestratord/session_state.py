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

from .workspace import Workspace

if TYPE_CHECKING:
    from .agent.task import AgentTask


@dataclass
class RunSubject:
    """Backend-neutral description of the work attached to a run.

    ``AgentSession.issue`` historically carried the tracker domain object all
    the way into the capability runner.  New generic callers use this small
    value object instead.  Since P6 the :class:`RunSession` field itself is
    named ``subject``; the deprecated ``issue`` name remains on
    :class:`RunSession` as a compat property so the issue-to-PR pipeline can
    migrate independently.
    """

    id: str
    identifier: str | None = None
    title: str = ""
    description: str = ""
    labels: list[str] = field(default_factory=list)
    url: str | None = None
    state: str | None = None
    author_login: str | None = None
    branch_name: str | None = None
    python_executable: str = ""
    priority: int | None = None


@dataclass
class RunSession:
    """One active backend-neutral run.

    The work slot is named ``subject``: a :class:`RunSubject` on the
    kernel/generic path, the tracker ``Issue`` on the issue-to-PR path.
    ``issue`` remains available as a deprecated compat property (P6,
    DESIGN §4.3/:523) so the ~170 read sites can migrate independently.
    """

    subject: Any  # Renamed from deprecated `issue` in P6 — see class docstring.
    workspace: Workspace
    task: "AgentTask | None" = None  # NEW: generic task abstraction
    # Orchestrator logical conversation metadata.  None remains valid for
    # legacy callers that construct a session directly.
    conversation_id: str | None = None
    parent_run_id: str | None = None
    backend_name: str | None = None
    backend_session_id: str | None = None
    stage_id: str | None = None
    stage_name: str | None = None
    branch_id: str | None = None
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
    # persistent IssueRecord before the run starts. P3 起存储于 ``business``
    # dict（DESIGN §4.3），此名保留为兼容 property。
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
    tool_count: int = 0
    # Cost telemetry: backends reporting real USD (clawcodex/claude
    # SESSION_COMPLETE payload "total_cost_usd") land in cost_usd;
    # backends reporting token usage (dsh payload "usage") land in
    # token_usage. Read by AgentTaskResult.cost_usd and
    # _update_run_diagnostics.
    cost_usd: float = 0.0
    token_usage: dict = field(default_factory=dict)
    started_at: float | None = None
    completed_at: float | None = None
    duration_ms: float | None = None
    verification_status: str | None = None
    verification_output: str | None = None
    report_path: str | None = None
    # Per-session cache for the tracker poll in ``_should_continue``.
    # Initialised by ``AgentRunner.run()`` from
    # ``agent_config.perf_should_continue_skip_turns``. When ``None`` the
    # runner falls back to the pre-cache behaviour of always polling.
    state_cache: Any | None = None
    # List of files git left in conflict state. Populated by
    # ``Orchestrator._prepare_rebase_session`` from
    # ``IssueRecord.conflict_files`` when ``run_kind == "agent_rebase"``.
    # P3 起存储于 ``business`` dict（DESIGN §4.3），此名保留为兼容 property。
    # Canonical path to ~/.orchestratord/tool-events/{run_id}/events.ndjson.
    tool_events_path: str | None = None
    # Session-transcript storage for conversation recording.
    _transcript_storage: Any | None = None
    _transcript_asst_text: str = ""
    _transcript_tool_uses: list[Any] = field(default_factory=list)
    _transcript_pending_results: dict[str, Any] = field(default_factory=dict)
    _transcript_result_order: list[str] = field(default_factory=list)
    attempt: int = 1
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
    previous_verification_error: str | None = None  # last run's pre-commit/verify failure output
    # Per-session followup messages queued by the operator via the
    # chat gateway or control socket.  Appended to the next turn's
    # prompt and cleared.  Backend-agnostic: works for all backends
    # because the runner rebuilds the prompt each turn.
    _pending_followups: list[str] = field(default_factory=list)
    _snapshot_provider: str = ""
    _snapshot_backend: str = ""
    _snapshot_model: str = ""
    _pause_gate: Any = None
    _on_pause_state_change: Any | None = None

    # Kept as an optional compatibility hook for callers that provide their
    # own snapshot implementation. It has no backend-specific default.
    _save_json_snapshot: Any = field(default=None, init=False, repr=False, compare=False)

    # Goal-mode state persisted alongside events for crash recovery.
    # Serialized from GoalManager.state.to_dict() on session close;
    # restored via GoalManager.restore() on resume.
    goal_state: dict | None = None

    # 业务私有载荷（DESIGN §4.3 / P3）：Kernel 与 Layer 1 对其内容完全透明。
    # clarification/conflict_files/attempt 类业务字段迁移于此，上方同名
    # property 为兼容 accessor；P4 起由 Application 经 RunContext.business 读写。
    # 注意：copy.copy(session) 会与本 dict 产生别名共享——任何新的会话拷贝点
    # 必须像 modes/debate.py 分支 fork 一样显式 ``dict(session.business)``
    # 重建容器，否则写操作会穿透到原会话。
    business: dict[str, Any] = field(default_factory=dict)

    # ------------------------------------------------------------------
    # Business payload accessors (DESIGN §4.3 / P3) — storage moved to the
    # ``business`` dict; the historical attribute names remain as read/write
    # properties so existing consumers stay source-compatible. Mechanism-domain
    # modules must not read these directly (tests/test_architecture.py).
    # ------------------------------------------------------------------

    @property
    def clarification_question(self) -> str | None:
        return self.business.get("clarification_question")

    @clarification_question.setter
    def clarification_question(self, value: str | None) -> None:
        self.business["clarification_question"] = value

    @property
    def clarification_answer(self) -> str | None:
        return self.business.get("clarification_answer")

    @clarification_answer.setter
    def clarification_answer(self, value: str | None) -> None:
        self.business["clarification_answer"] = value

    @property
    def clarification_source(self) -> str | None:
        return self.business.get("clarification_source")

    @clarification_source.setter
    def clarification_source(self, value: str | None) -> None:
        self.business["clarification_source"] = value

    @property
    def conflict_files(self) -> tuple[str, ...] | None:
        return self.business.get("conflict_files")

    @conflict_files.setter
    def conflict_files(self, value: tuple[str, ...] | None) -> None:
        self.business["conflict_files"] = value

    @property
    def summary_comment_id(self) -> str | None:
        return self.business.get("summary_comment_id")

    @summary_comment_id.setter
    def summary_comment_id(self, value: str | None) -> None:
        self.business["summary_comment_id"] = value

    @property
    def issue_attempt(self) -> int:
        return self.business.get("issue_attempt", 1)

    @issue_attempt.setter
    def issue_attempt(self, value: int) -> None:
        self.business["issue_attempt"] = value

    @property
    def followup_attempt(self) -> int:
        return self.business.get("followup_attempt", 1)

    @followup_attempt.setter
    def followup_attempt(self, value: int) -> None:
        self.business["followup_attempt"] = value

    # ------------------------------------------------------------------
    # Deprecated work-slot alias (P6, DESIGN §4.3/:523): the dataclass
    # field is ``subject``; the historical ``issue`` name remains as a
    # read/write property so its read sites migrate independently.
    # ------------------------------------------------------------------

    @property
    def issue(self) -> Any:
        """Deprecated alias of :attr:`subject` (P6 field rename)."""
        return self.subject

    @issue.setter
    def issue(self, value: Any) -> None:
        self.subject = value

    def business_state(self) -> dict[str, Any]:
        """业务载荷完整快照（含未设置键的机制默认值）。

        供分支 fork（modes/debate 的 SimpleNamespace 回退路径）复制完整
        键集使用——property 不进入 ``vars(session)``，此方法补齐缺省键。
        """
        return {
            "clarification_question": self.clarification_question,
            "clarification_answer": self.clarification_answer,
            "clarification_source": self.clarification_source,
            "conflict_files": self.conflict_files,
            "summary_comment_id": self.summary_comment_id,
            "issue_attempt": self.issue_attempt,
            "followup_attempt": self.followup_attempt,
        }


# Backwards-compatible public name used by the issue-to-PR application.
AgentSession = RunSession


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
    # How many times a tracker-miss/fetch-failure has
    # re-queued this item (ceiling in Orchestrator._retry_requeue_limit).
    requeue_count: int = 0
