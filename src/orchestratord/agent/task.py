"""AgentTask — generic work-unit abstraction for the orchestration layer.

AgentTask decouples agent execution from the business application that
requested it. Different workflow kinds populate opaque context fields but
share the same ``AgentTaskRunner.run_task()`` interface.

See ``DESIGN_agent_task_abstraction.md`` for the full architecture.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


@dataclass
class AgentTask:
    """A unit of work dispatched to an AI coding agent.

    AgentTask is the generic abstraction that sits between "something
    that needs doing" and the agent execution layer.  It intentionally
    knows nothing about issues, pull requests, git, or trackers.

    The following names are compatibility examples owned by the bundled
    issue-to-PR application; the capability layer does not interpret them:

    ``kind="issue"``
        ``title`` = issue title, ``description`` = issue body,
        ``context["issue_id"]`` = tracker ID,
        ``context["issue_identifier"]`` = HUMAN-READABLE key

    ``kind="workflow_stage"``
        ``title`` = "[phase] stage name", ``description`` = stage prompt,
        ``context["stage_id"]``, ``context["phase"]``,
        ``context["parent_issue"]`` = original Issue (for template rendering)

    ``kind="review_followup"``
        ``title`` = issue title, ``description`` = review feedback,
        ``context["pull_request"]`` = PullRequestRef,
        ``context["feedback"]`` = list[PullRequestFeedback]

    ``kind="agent_rebase"``
        ``title`` = issue title, ``description`` = rebase instructions,
        ``context["branch_name"]``, ``context["base_branch"]``,
        ``context["conflict_files"]``

    ``kind="ci_fix"`` (future)
        ``title`` = "CI Failure: <job>", ``description`` = build log,
        ``context["job_name"]``, ``context["build_url"]``
    """

    # ── Identity ──────────────────────────────────────────────
    id: str
    kind: str = "generic"
    # Stable orchestrator-level logical conversation identity.  This is
    # deliberately independent from a backend's native session key.
    conversation_id: str | None = None

    # ── Core content ──────────────────────────────────────────
    title: str = ""
    description: str = ""

    # ── Extensible context (workflow-specific) ────────────────
    # Keys are defined per-kind.  Layer 1 treats this as opaque
    # data for template rendering.  Layer 2 populates it.
    context: dict[str, Any] = field(default_factory=dict)

    # ── Workspace ─────────────────────────────────────────────
    workspace_path: str = ""

    # ── Metadata ──────────────────────────────────────────────
    labels: list[str] = field(default_factory=list)
    priority: int | None = None
    attempt: int = 1
    previous_run_ids: list[str] = field(default_factory=list)

    # ── Lifecycle hints (overrides per-workflow defaults) ─────
    max_turns: int | None = None
    timeout_seconds: float | None = None

    # ── Prompt override ───────────────────────────────────────
    # When set, bypasses PromptBuilder entirely.  Used by
    # StageRunner for synthetic stage prompts.
    prompt_override: str | None = None

    def to_template_dict(self) -> dict[str, Any]:
        """Render-friendly dict for Jinja2 templates.

        Templates can access ``{{ task.title }}``, ``{{ task.kind }}``,
        and any ``{{ task.context.xxx }}`` key.
        """
        return {
            "id": self.id,
            "kind": self.kind,
            "conversation_id": self.conversation_id,
            "title": self.title,
            "description": self.description,
            "labels": self.labels,
            "priority": self.priority,
            "attempt": self.attempt,
            "context": dict(self.context),
        }


@dataclass
class AgentTaskResult:
    """Result of executing an AgentTask through the orchestration layer.

    This is the *only* output from Layer 1.  Layer 2 interprets it
    according to the task kind and decides what to do next (git sync,
    PR update, retry, etc.).
    """

    task_id: str
    kind: str = "generic"
    conversation_id: str | None = None

    # ── Terminal status ───────────────────────────────────────
    # One of: "completed", "failed", "stagnation", "loop_detected",
    #         "max_turns_exceeded", "read_only_loop", "premise_not_met",
    #         "no_changes_produced", "empty_branch_no_commits",
    #         "interrupted", "rate_limited"
    status: str = "completed"

    # ── Output ────────────────────────────────────────────────
    output_text: str = ""
    turn_count: int = 0
    tool_count: int = 0

    # ── End reason (detailed) ─────────────────────────────────
    session_end_reason: str | None = None
    session_end_summary: str = ""

    # ── Verification ──────────────────────────────────────────
    verification_status: str | None = None
    verification_output: str | None = None

    # ── Artifacts ─────────────────────────────────────────────
    report_path: str | None = None
    run_id: str | None = None

    # ── Cost ──────────────────────────────────────────────────
    cost_usd: float = 0.0

    # ── Error ─────────────────────────────────────────────────
    error: str | None = None

    @property
    def is_success(self) -> bool:
        return self.status == "completed"

    @property
    def is_terminal_failure(self) -> bool:
        return self.status in (
            "failed",
            "premise_not_met",
            "no_changes_produced",
            "empty_branch_no_commits",
        )


class ProgressEventKind(str, Enum):
    TEXT = "text"
    TEXT_DELTA = "text_delta"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    TURN_COMPLETE = "turn_complete"
    SESSION_COMPLETE = "session_complete"
    ERROR = "error"


@dataclass
class ProgressEvent:
    """Emitted by Layer 1 during agent execution.

    Layer 2 subscribes to these for dashboards, IM notifications,
    and state-journal writes — without Layer 1 knowing about any of
    those consumers.
    """

    kind: ProgressEventKind
    task_id: str
    turn_number: int | None = None
    tool_count: int | None = None
    text: str = ""
    tool_name: str = ""
    call_id: str = ""
    message: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
