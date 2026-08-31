"""SPI: AgentBackend — 后端工厂协议（Backend Factory Protocol）

Each backend package implements one AgentBackend and registers it via
entry_points [orchestratord.backends].  The orchestration core discovers
backends through entry_points and never imports backend packages directly
(CI-enforced).

Timeout semantics (DESIGN_graded_timeouts_and_resume.md §1):

SessionSpec carries **five independent** timeout fields. Each backend
decides which subset to honor and how (see backend_runner.py §3.3 for
the per-backend mapping). The five are:

┌─────────────────────────┬──────────────────────────────────────────────┐
│ total_timeout_s         │ Hard upper bound on the entire session run    │
│                         │ (run-level watchdog). Default 1800s.         │
│ handshake_timeout_s     │ Start handshake → first event emission.       │
│                         │ Default 30s. Catches "agent hung at boot".   │
│ first_turn_timeout_s    │ Handshake OK → first TURN_COMPLETE.           │
│                         │ Default 120s. Catches "agent started but is   │
│                         │ thinking forever".                            │
│ inactivity_timeout_s    │ Max interval between consecutive token        │
│                         │ emissions (stall detection). Default 300s.    │
│                         │ Catches "agent producing nothing for N        │
│                         │ seconds".                                     │
│ idle_watchdog_timeout_s │ Max session idle (no events of any kind).      │
│                         │ Default == total_timeout_s. Catches "agent    │
│                         │ deadlocked / pipe stalled".                   │
└─────────────────────────┴──────────────────────────────────────────────┘

Backward compatibility: ``timeout_s`` and ``stall_timeout_s`` are kept
as **deprecated** aliases. When a caller sets them but not the new
fields, ``__post_init__`` folds them into ``total_timeout_s`` /
``inactivity_timeout_s`` respectively. New code should use the five
explicit fields.
"""

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from orchestratord.spi.approval import ApprovalPolicy
from orchestratord.spi.capabilities import BackendCapabilities
from orchestratord.spi.session import AgentSession


@dataclass
class SessionSpec:
    """Parameters for creating a new agent session.

    Generic fields are carried directly on the dataclass. Backend-specific
    values are passed through ``extra`` and interpreted only by that backend.
    """

    cwd: str
    system_prompt: str | None = None
    model: str | None = None
    provider: str | None = None
    base_url: str | None = None
    api_key: str | None = None
    cordis: str | None = None
    runtime_bin: str | None = None
    permission_mode: str | None = None
    tools_allow: list[str] | None = None
    tools_deny: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    resume_session_id: str | None = None
    max_turns: int | None = None

    # ------------------------------------------------------------------
    # Deprecated timeout aliases (DESIGN_graded_timeouts_and_resume.md §1.2)
    #
    # ``timeout_s`` was the legacy single-shot "run timeout". It is now
    # an alias for ``total_timeout_s``. ``stall_timeout_s`` was the
    # legacy stall watchdog; it is now an alias for
    # ``inactivity_timeout_s``. New callers should set the explicit
    # five-field set below instead.
    # ------------------------------------------------------------------
    timeout_s: float | None = None            # DEPRECATED: alias for total_timeout_s
    stall_timeout_s: float | None = None     # DEPRECATED: alias for inactivity_timeout_s
    stall_warn_s: float | None = None        # DEPRECATED: warning threshold

    # ------------------------------------------------------------------
    # 5-level timeout classification
    # ------------------------------------------------------------------
    total_timeout_s: float | None = None        # run-level watchdog
    handshake_timeout_s: float | None = None    # start → first event
    first_turn_timeout_s: float | None = None   # first event → first TURN_COMPLETE
    inactivity_timeout_s: float | None = None   # gap between token emissions
    idle_watchdog_timeout_s: float | None = None  # gap between any events

    run_id: str | None = None
    debug_log_path: str | None = None
    approval: ApprovalPolicy | None = None
    extra: dict[str, Any] = field(default_factory=dict)
    goal_condition: str | None = None
    goal_max_turns: int | None = None
    goal_subgoals: list[str] | None = None

    def __post_init__(self) -> None:
        """Fold deprecated timeout aliases into the new fields and validate.

        Alias folding (backward compat):
          * ``timeout_s``         → ``total_timeout_s`` (when latter is None)
          * ``stall_timeout_s``   → ``inactivity_timeout_s`` (when latter is None)

        Validation invariants (DESIGN §1.4):
          * sub-timeouts (handshake, first_turn, inactivity) must each
            not exceed ``total_timeout_s`` — the run watchdog must
            envelop every phase
          * ``idle_watchdog_timeout_s`` must not be smaller than
            ``total_timeout_s`` — a watchdog shorter than the run
            itself is meaningless
        """
        # 1. Fold deprecated aliases.
        if self.timeout_s is not None and self.total_timeout_s is None:
            self.total_timeout_s = self.timeout_s
        if (
            self.stall_timeout_s is not None
            and self.inactivity_timeout_s is None
        ):
            self.inactivity_timeout_s = self.stall_timeout_s

        # 2. Validate invariants (only when callers actually set values).
        if self.total_timeout_s is not None:
            sub_values = [
                v
                for v in (
                    self.handshake_timeout_s,
                    self.first_turn_timeout_s,
                    self.inactivity_timeout_s,
                )
                if v is not None
            ]
            for sub in sub_values:
                if sub > self.total_timeout_s:
                    raise ValueError(
                        f"sub-timeout ({sub}s) exceeds total_timeout_s "
                        f"({self.total_timeout_s}s) — the run watchdog "
                        "must envelop every phase"
                    )
            if (
                self.idle_watchdog_timeout_s is not None
                and self.idle_watchdog_timeout_s < self.total_timeout_s
            ):
                raise ValueError(
                    "idle_watchdog_timeout_s must be >= total_timeout_s "
                    f"(got idle={self.idle_watchdog_timeout_s}s, "
                    f"total={self.total_timeout_s}s) — a watchdog shorter "
                    "than the run itself is meaningless"
                )


@runtime_checkable
class AgentBackend(Protocol):
    """Factory for agent sessions.

    Each backend implements one of these and registers it via
    ``[orchestratord.backends]`` entry_points.
    """

    name: str
    display_name: str

    def capabilities(self) -> BackendCapabilities:
        """Return the backend's capability bits."""
        ...

    def preflight(self, spec: SessionSpec) -> None:
        """Validate local dependencies and configuration before a session starts.

        Raise ``RuntimeError`` with actionable guidance when the backend cannot
        run in the supplied environment. The check must not contact an LLM
        provider or create a long-lived session.
        """
        ...

    def create_session(self, spec: SessionSpec) -> AgentSession:
        """Create a new session (or resume one if spec.resume_session_id is set)."""
        ...

    def get_task_registry(self) -> Any | None:
        """Return a runtime task registry for real-time message injection.

        The registry enables ``queue_pending_message`` to fire at
        ``ToolResult`` boundaries during agent execution.  Return
        ``None`` if the backend does not support runtime task tracking.
        """
        ...

    def dispose(self) -> None:
        """Release backend-level resources (workers, pools, etc.)."""
        ...
