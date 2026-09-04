"""Polling engine — agent-agnostic workflow engine.

Orchestrator — agent-agnostic workflow engine.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .backend_runner import BackendRunner
from .agent.task import AgentTask, AgentTaskResult
from .agent.runner import AgentTaskRunner
from .issue_registry.task_mapping import issue_to_agent_task
from .git.utils import (
    get_default_branch,
    get_file_status,
    get_repo_root,
    run_git as _run_git,
)
from .session_state import AgentSession, RetryItem
from .runner_utils import _apply_pause_session, _apply_resume_session
from .config.schema import WorkflowConfig
from .debug_log import append_debug_event
from .events import EventLevel
from .git.sync import (
    GitSyncPostCommitError,
    GitSyncService,
    HookFailedError,
    PRRebaseResult,
    VerificationFailed,
    rebase_for_pr,
)
from .issue_registry.issue import Issue
from .issue_registry import IssueRegistry, IssueStatus
from .mode_router import HeuristicRouter, LLMRouter, Router
from .mode_selector import ModeSelector
from . import modes as _modes
from .modes.base import DEFAULT_MODE, ModeDecision
from .modes.coordinator import CoordinatorModeRunner
from .modes.debate import DebateModeRunner
from .modes.pipeline import PipelineModeRunner
from .modes.single import SingleModeRunner
from .modes.swarm import SwarmModeRunner
from .failure_messages import (
    FailureContext,
    empty_branch_message,
    end_reason_guidance,
    generic_failure_message,
    issue_summary_guidance,
    loop_detected_message,
    max_turns_message,
    no_changes_message,
    post_commit_failed_message,
    rate_limit_message,
    stagnation_message,
    timeout_message,
    verification_failed_message,
)
from .premise_check import format_cannot_proceed_comment, read_cannot_proceed
from .prompt_builder import PromptBuilder
from .repro_gate import (
    ReproGateResult,
    append_repro_hint,
    build_repro_prompt,
    evaluate_repro_gate,
    format_repro_gate_comment,
)
from .review_feedback import ReviewFeedbackService, ReviewFollowup
from .rules_learner import RuleEngine, RuleStore
from .status_dashboard import SessionStatus, StatusDashboard
from .tracker import (
    Command,
    CommandIntentCapability,
    Intent,
    PullRequestCapability,
    PullRequestFeedback,
    PullRequestFeedbackCapability,
    PullRequestMaintenanceCapability,
    PullRequestRef,
    TrackerAdapter,
    command_to_intent,
    merge_intents,
    merge_intents_with_cli,
    supports,
)
from .workspace import WorkspaceManager
from .paths import AUDIT_LOG, ORCHESTRATORD_BASE

if TYPE_CHECKING:
    from .tracker import CommandIntent
    from orchestratord.spi.backend import AgentBackend

logger = logging.getLogger(__name__)

_CONTINUATION_RETRY_DELAY_MS = 1_000
_FAILURE_RETRY_BASE_MS = 10_000
# End reasons that mean SUCCESS — the failure-guidance block must skip
# them (they are not in the failure guidance table and would otherwise
# fall back to the generic "未知错误" fallback).
_SUCCESS_END_REASONS = frozenset({"success"})

# End reasons produced by explicit operator action. They are
# terminal states — the auto-retry loop must not revive them.
_NON_RETRYABLE_END_REASONS = frozenset({
    "operator_stop",
    "operator_takeover",
    "operator_stopped",
})

# End reasons produced by explicit operator action. They are
# terminal states — the auto-retry loop must not revive them.
_NON_RETRYABLE_END_REASONS = frozenset({
    "operator_stop",
    "operator_takeover",
    "operator_stopped",
})


def _operator_failure_detail(exc: BaseException) -> str:
    """Return a concise failure detail suitable for IM and registry records."""
    raw = " ".join(str(exc).split())
    body_detail = _extract_error_message_from_body(raw)
    if body_detail:
        status_code = _extract_status_code(raw)
        if raw.startswith("request_failed") and status_code:
            return f"request_failed status={status_code}: {body_detail}"
        return body_detail
    return raw or exc.__class__.__name__


def _extract_status_code(text: str) -> str | None:
    for part in text.split():
        if part.startswith("status="):
            status = part.removeprefix("status=").strip()
            if status:
                return status
    return None


def _extract_error_message_from_body(text: str) -> str | None:
    marker = "body="
    marker_index = text.find(marker)
    if marker_index < 0:
        return None
    body = text[marker_index + len(marker) :].strip()
    if not body:
        return None
    try:
        payload, _ = json.JSONDecoder().raw_decode(body)
    except ValueError:
        return None
    return _extract_error_message(payload)


def _extract_error_message(payload: Any) -> str | None:
    if isinstance(payload, dict):
        for key in (
            "error_message",
            "message",
            "error_description",
            "detail",
        ):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return " ".join(value.split())
        error = payload.get("error")
        if isinstance(error, str) and error.strip():
            return " ".join(error.split())
        nested = _extract_error_message(error)
        if nested:
            return nested
        errors = payload.get("errors")
        if isinstance(errors, list):
            for item in errors:
                nested = _extract_error_message(item)
                if nested:
                    return nested
    return None


@dataclass
class OrchestratorState:
    """Runtime state for the orchestrator polling loop."""

    poll_interval_ms: int = 30_000
    max_concurrent_agents: int = 10
    next_poll_due_at_ms: float | None = None
    poll_check_in_progress: bool = False
    running: dict[str, AgentSession] = field(default_factory=dict)
    completed: set[str] = field(default_factory=set)
    failed: set[str] = field(default_factory=set)
    pending_review: set[str] = field(default_factory=set)  # awaiting human review
    claimed: set[str] = field(default_factory=set)
    retry_queue: list[RetryItem] = field(default_factory=list)
    retry_attempts: dict[str, int] = field(default_factory=dict)
    # Throttle marker for the optional PR conflict scan. Wall-clock
    # seconds (not ms) of the last scan — compared against
    # ``time.monotonic()`` so a backwards clock jump is benign.
    pr_conflict_scan_last_run: float = 0.0
    codex_totals: dict[str, int] = field(
        default_factory=lambda: {
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
            "seconds_running": 0,
        }
    )


class Orchestrator:
    """Polling engine — GenServer equivalent in Python."""

    def __init__(
        self,
        workflow: WorkflowConfig,
        tracker: TrackerAdapter,
        workspace: WorkspaceManager,
        agent_runner: BackendRunner,
        status_dashboard: StatusDashboard | None = None,
        *,
        # SPI AgentBackend (added for orchestration_subsystem call-site parity).
        # The subsystem passes ``backend=self._backend`` since the SPI refactor;
        # origin's Orchestrator drives ``agent_runner`` directly, so this is
        # accepted-and-ignored to keep that call site working.
        backend: Any = None,
        stage_runners: dict[str, "BackendRunner"] | None = None,
        workflow_yaml_path: str | None = None,
        # Issue-clarifier provider factory (added for orchestration_subsystem
        # call-site parity).  Accepted but unused at HEAD — the subsystem wires
        # it on its own side; Orchestrator's existing clarification path stays
        # unchanged.
        clarifier_provider_factory: Any = None,
        asciicast_capture: Any = None,
    ) -> None:
        self.workflow = workflow
        self.tracker = tracker
        self.workspace = workspace
        self.agent_runner = agent_runner
        self.stage_runners = stage_runners or {}
        # SPI AgentBackend — used to back the issue clarifier's single-turn
        # analysis (DESIGN_decoupling_clawcodex.md §2.6).  ``None`` keeps the
        # legacy provider-factory clarifier path.
        self._backend = backend
        # F-REC: optional asciicast capture. When set, every per-session
        # :class:`CompositeProgressSink` built by :meth:`_build_session_sink`
        # registers an :class:`AsciicastSink` so the agent's progress
        # events land in the same ``.cast`` file as the other adapters.
        # ``None`` (the default) preserves the existing behaviour — no
        # recording happens, no extra import cost.
        self.asciicast_capture = asciicast_capture
        # Collaboration modes — Phase 2 wires the registry +
        # ``ModeSelector`` + ``Router`` based on the ``modes:`` YAML
        # section. ``ModesConfig`` defaults (no router, only "single"
        # enabled) preserve byte-identical behavior for workflows that
        # don't opt in.
        self._register_collaboration_modes(workflow, agent_runner)
        self._mode_selector = self._build_mode_selector(workflow)
        self._workflow_yaml_path = workflow_yaml_path
        self._workflow_orchestrator = None

        # The StateJournalWriter existed but was never
        # instantiated anywhere, so the visualizer's orchestrator dashboard
        # (reads ``~/.orchestratord/reports/run_*/state_journal.ndjson``) always
        # showed "no runs". One journal per daemon lifetime; writes are
        # fire-and-forget and must never affect orchestration.
        self._viz_journal = None
        try:
            from datetime import datetime, timezone

            from .state_journal import StateJournalWriter

            journal_run_id = "run_" + datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
            self._viz_journal = StateJournalWriter(
                ORCHESTRATORD_BASE / "reports" / journal_run_id,
                journal_run_id,
            )
            self._viz_journal.write_event(
                {
                    "type": "orchestrator_start",
                    "workflow": workflow_yaml_path or "",
                }
            )
        except Exception:
            logger.exception("state journal init failed — dashboard disabled")
            self._viz_journal = None

        # 初始化声明式工作流引擎
        if workflow_yaml_path:
            from .workflow_orchestrator import WorkflowOrchestrator

            self._workflow_orchestrator = WorkflowOrchestrator(
                workflow_config=workflow,
                workflow_yaml_path=workflow_yaml_path,
                agent_runner=agent_runner,
                tracker=tracker,
                status_dashboard=status_dashboard,
                diagnostics_callback=self._update_run_diagnostics,
            )
            logger.info(
                "Workflow engine enabled: %s (%s, %d stages)",
                workflow_yaml_path,
                self._workflow_orchestrator.schema.name,
                len(self._workflow_orchestrator.schema.stages),
            )

        self.status_dashboard = status_dashboard or StatusDashboard()
        self._agent_config = workflow.agent
        # IM-side channel adapter (e.g. FeishuAppChannelAdapter). When
        # set, :meth:`_build_session_sink` attaches a
        # :class:`FeishuActivitySink` so the bot's reactions + placeholder
        # progress card track the agent lifecycle for users on IM. None
        # → activity sink disabled (default; not every deployment has an IM
        # channel even if ``im_event_deliver`` is wired).
        self.im_channel_adapter: Any = None
        self._validate_workspace_strategy()
        self.git_sync = GitSyncService(
            tracker,
            workflow.tracker.branch_prefix,
            workflow.workspace.gitignore_patterns,
            workflow.agent,
            workflow.hooks,
            git_username=workflow.workspace.git_username,
            git_email=workflow.workspace.git_email,
            upstream_clone_url=workflow.workspace.upstream_clone_url,
            fork_clone_url=workflow.workspace.repo_clone_url,
            pr_template=workflow.pr_template,
        )
        self._state = OrchestratorState(
            poll_interval_ms=workflow.polling.interval_ms,
            max_concurrent_agents=workflow.agent.max_concurrent_agents,
        )
        self._semaphore = asyncio.Semaphore(workflow.agent.max_concurrent_agents)
        self._shutdown_event = asyncio.Event()
        self._tasks: set[asyncio.Task] = set()
        # Root-cause fix: map issue_id → asyncio.Task so the stop
        # command can cancel a specific running issue by task.cancel().
        self._issue_tasks: dict[str, asyncio.Task] = {}
        # Store workflow path for metadata
        self._workflow_path: str | None = getattr(workflow, "source_path", None) or getattr(
            workflow, "_source_path", None
        )
        self._dynamic_tracker_config_mtime_ns: int | None = self._workflow_mtime_ns()
        # Workspace root for control command polling
        workspace_root = Path(workspace.config.root)
        self._workspace_root = workspace_root
        # Persistent issue→commit→PR mapping (persists across restarts)
        registry_path = workspace_root / ".orchestratord_issue_registry.json"
        self._registry = IssueRegistry(registry_path)

        # Write orchestrator metadata for CLI discovery
        self._metadata_started_at = time.time()
        from .workspace_locator import write_orchestrator_metadata

        write_orchestrator_metadata(
            workspace_root=workspace_root,
            workflow_path=self._workflow_path,
            started_at=self._metadata_started_at,
        )

        # Clarification handling (three-channel flow)
        clarification_queue_path = workspace_root / ".orchestratord_clarification_queue.json"
        from .issue_clarifier.queue import ClarificationQueue

        self._clarification_queue = ClarificationQueue(clarification_queue_path)

        from .issue_clarifier.resolver import (
            ClarificationConfig,
            ClarificationResolver,
            _DEFAULT_MAX_QUESTIONS_PER_ISSUE,
            _DEFAULT_SIMULTANEOUS_GRACE_MS,
            _DEFAULT_TIMEOUT_AUTHOR_SECONDS,
            _DEFAULT_TIMEOUT_LOCAL_SECONDS,
        )

        self._clarification_resolver = ClarificationResolver(
            clarification_queue=self._clarification_queue,
            tracker=tracker,
            config=ClarificationConfig(
                enabled=getattr(workflow.agent, "clarification_enabled", True),
                timeout_local_seconds=getattr(
                    workflow.agent, "clarification_timeout_local", _DEFAULT_TIMEOUT_LOCAL_SECONDS
                ),
                timeout_author_seconds=getattr(
                    workflow.agent, "clarification_timeout_author", _DEFAULT_TIMEOUT_AUTHOR_SECONDS
                ),
                max_questions_per_issue=getattr(
                    workflow.agent, "max_questions_per_issue", _DEFAULT_MAX_QUESTIONS_PER_ISSUE
                ),
                operator_priority=getattr(workflow.agent, "clarification_operator_priority", True),
                simultaneous_grace_ms=getattr(
                    workflow.agent, "clarification_simultaneous_grace_ms", _DEFAULT_SIMULTANEOUS_GRACE_MS
                ),
                escalation=getattr(workflow.agent, "clarification_escalation", "skip"),
            ),
        )
        self._clarification_gate = None
        clarifier_config = getattr(workflow, "clarifier", None)
        if clarifier_config is not None and bool(getattr(clarifier_config, "enabled", False)):
            from .issue_clarifier import ClarifierCache, IssueClarifierService
            from .issue_clarifier.gate import IssueClarificationGate

            cache = ClarifierCache(
                workspace_root / ".orchestratord_issue_clarifier_cache.json",
                enabled=bool(getattr(clarifier_config, "cache_enabled", True)),
            )

            def _build_clarifier_provider() -> Any:
                if self._clarifier_provider_factory is None:
                    raise RuntimeError(
                        "Issue clarification requires an explicitly injected provider factory."
                    )
                return self._clarifier_provider_factory()

            def _build_clarifier_backend_spec() -> Any:
                """Single-turn plan-only session for clarity analysis
                (DESIGN_decoupling_clawcodex.md §2.6)."""
                from .spi.backend import SessionSpec

                return SessionSpec(
                    cwd=str(workspace_root),
                    max_turns=1,
                    permission_mode="plan",
                )

            service = IssueClarifierService(
                config=clarifier_config,
                cache=cache,
                provider_factory=_build_clarifier_provider,
                backend=self._backend,
                backend_spec_factory=_build_clarifier_backend_spec,
                model=getattr(workflow.agent, "model", None),
            )
            self._clarification_gate = IssueClarificationGate(
                service=service,
                resolver=self._clarification_resolver,
                registry=self._registry,
                config=clarifier_config,
                tracker=self.tracker,
                workspace_focus_callback=self._compute_workspace_focus_for_clarifier,
            )
            logger.info(
                "Issue clarifier enabled (block=%s, author_first=%s)",
                clarifier_config.block_on_unclear,
                clarifier_config.author_first,
            )
        self._progress_context = None
        # P3 IM event bridge: if set (by the daemon wiring a gateway deliver),
        # :meth:`_build_session_sink` attaches an :class:`OrchestratorEventEmitter`
        # so key orchestrator events push to IM. None → IM events disabled.
        self.im_event_deliver: "object | None" = None
        self.im_event_channel: str = ""
        self._im_emitters: dict = {}
        # Do NOT keep a single :class:`ProgressReporter` here.
        # Per-session progress is fanned out via
        # :meth:`_build_session_sink` (a fresh
        # :class:`CompositeProgressSink` rooted in a private
        # :class:`ToolContextProgressSink`) so concurrent issues can no
        # longer share ``_current_task_id`` / ``_phase_count`` state.
        # The shared ``_progress_context`` stays because every
        # per-session :class:`ToolContextProgressSink` writes into the
        # same ``ToolContext.tasks[id].metadata.progress_stages`` dict.

    def _build_session_sink(self, task_id: str) -> Any:
        """Build a fresh :class:`CompositeProgressSink` for one session.

        The returned sink is bound to ``task_id`` and owns a private
        :class:`ToolContextProgressSink` instance. Two sinks built for
        different task ids share the underlying ``ToolContext`` (so
        progress stages land in the right place) but have independent
        phase counters, eliminating the legacy single-instance
        cross-talk.

        Future issues (PR review auto-fix sink, retry label sink)
        can register additional sinks on the returned composite via
        :meth:`CompositeProgressSink.add` without touching
        :class:`AgentRunner` or ``progress_reporter.py``.
        """
        from .sinks.progress import (
            CompositeProgressSink,
            ToolContextProgressSink,
        )

        inner = ToolContextProgressSink(
            task_id=task_id,
            workflow_phases=self.workflow.agent.phases,
            fallback_to_phase_step=bool(self.workflow.agent.fallback_to_phase_step),
            context=getattr(self, "_progress_context", None),
        )
        composite = CompositeProgressSink([inner])
        # P3: attach the IM event emitter when a deliver callback is wired.
        if getattr(self, "im_event_deliver", None) is not None:
            from .sinks.channel import ChannelProgressSink
            from .events import OrchestratorEvent, OrchestratorEventEmitter

            channel_sink = ChannelProgressSink(self.im_event_deliver)
            emitter = OrchestratorEventEmitter(
                task_id=task_id,
                sinks=[channel_sink],
            )
            # Stash for explicit emit() at blind-spot call sites.
            self._im_emitters[task_id] = emitter
            composite.add(emitter)
            emitter.emit(
                OrchestratorEvent(
                    event_type="issue.started",
                    issue_id=task_id,
                    level=EventLevel.INFO,
                    message="任务已启动",
                    payload=self._issue_payload_for_task_id(task_id),
                )
            )
        # IM-side activity sink: attach only through the public card-update
        # protocol and its declared capability. Channel-specific caches and
        # loop internals stay behind the adapter boundary.
        im_adapter = getattr(self, "im_channel_adapter", None)
        capabilities = getattr(im_adapter, "capabilities", None)
        declared_capabilities = getattr(capabilities, "_capabilities", capabilities)
        try:
            has_card_update = any(
                str(getattr(capability, "value", capability)).lower() == "card_update"
                for capability in declared_capabilities
            )
        except TypeError:
            has_card_update = False
        if im_adapter is not None and has_card_update \
                and callable(getattr(im_adapter, "send_placeholder_card", None)) \
                and callable(getattr(im_adapter, "update_progress_card", None)):
            from .sinks.feishu_activity import FeishuActivitySink

            phases_total = (
                len(self.workflow.agent.phases)
                if getattr(self.workflow.agent, "phases", None)
                else None
            )
            activity_sink = FeishuActivitySink(
                task_id=task_id,
                feishu_adapter=im_adapter,
                clock=time.time,
                status_dashboard=getattr(self, "status_dashboard", None),
                phases_total=phases_total,
            )
            composite.add(activity_sink)
        # F-REC: when a capture handle is wired (typically by the
        # the report CLI or by ``report_writer.write`` dual-
        # write), attach an :class:`AsciicastSink` so phase / session
        # markers land in the .cast. Defensive try/except mirrors the
        # IM-sink block above — recording failures must never block
        # the live orchestrator.
        capture = getattr(self, "asciicast_capture", None)
        if capture is not None:
            try:
                from .sinks.asciicast import AsciicastSink

                phases_total = (
                    len(self.workflow.agent.phases)
                    if getattr(self.workflow.agent, "phases", None)
                    else None
                )
                composite.add(
                    AsciicastSink(
                        capture,
                        task_id=task_id,
                        phases_total=phases_total,
                    )
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "asciicast sink attach failed (task_id=%s): %s",
                    task_id,
                    exc,
                )
        return composite

    def _emit_im_event(
        self,
        issue_id: str,
        event_type: str,
        level: EventLevel,
        message: str = "",
        payload: dict[str, Any] | None = None,
    ) -> None:
        """Emit one key orchestrator event to IM if the bridge is enabled."""
        issue_id = issue_id or "orchestrator"
        emitters = getattr(self, "_im_emitters", {})
        emitter = emitters.get(issue_id)
        if emitter is None:
            deliver = getattr(self, "im_event_deliver", None)
            if deliver is None:
                return
            from .sinks.channel import ChannelProgressSink
            from .events import OrchestratorEventEmitter

            emitter = OrchestratorEventEmitter(issue_id, sinks=[ChannelProgressSink(deliver)])
            emitters[issue_id] = emitter
            self._im_emitters = emitters
        from .events import OrchestratorEvent

        emitter.emit(
            OrchestratorEvent(
                event_type=event_type,
                issue_id=issue_id,
                level=level,
                message=message,
                payload=dict(payload or {}),
            )
        )

    def _issue_payload_for_task_id(self, task_id: str) -> dict[str, Any]:
        """Build a payload for issue.started when only the task_id is known.

        At sink-build time the Issue object is on ``session.issue`` but
        ``_build_session_sink`` receives only the task_id. We look up the
        registry record for branch/identifier, and the tracker for repo.
        """
        payload: dict[str, Any] = {}
        registry = getattr(self, "_registry", None)
        record = registry.get(task_id) if registry and task_id else None
        if record is not None:
            if getattr(record, "issue_identifier", None):
                payload["title"] = record.issue_identifier
            if getattr(record, "branch_name", None):
                payload["branch"] = record.branch_name
        repo = self._repo_label()
        if repo:
            payload["repo"] = repo
        return payload

    def _repo_label(self) -> str:
        """Build a 'owner/repo' label from the tracker, or '' if unavailable."""
        tracker = getattr(self, "tracker", None)
        if tracker is None:
            return ""
        owner = getattr(tracker, "owner", None)
        repo = getattr(tracker, "repo", None)
        if owner and repo:
            return f"{owner}/{repo}"
        return ""

    def _issue_payload(self, issue: Issue, **extra: Any) -> dict[str, Any]:
        """Build a rich payload dict for IM events from an Issue + extras.

        Centralizes the issue title / branch / repo context so every emit
        call site gets consistent enrichment without repeating field
        extraction. ``extra`` kwargs are merged in (e.g. commit=, pr=,
        verification=, attempts=).
        """
        payload: dict[str, Any] = {}
        title = getattr(issue, "title", None)
        if title:
            payload["title"] = title
        branch = getattr(issue, "branch_name", None)
        if branch:
            payload["branch"] = branch
        repo = self._repo_label()
        if repo:
            payload["repo"] = repo
        payload.update({k: v for k, v in extra.items() if v is not None})
        return payload

    def _session_payload(self, session: Any, **extra: Any) -> dict[str, Any]:
        """Build a rich payload from an AgentSession + extras.

        Reads issue title/branch, repo, verification status, PR url, and
        commit sha from the session/registry, then merges ``extra``.
        """
        issue = getattr(session, "issue", None)
        payload: dict[str, Any] = {}
        if issue is not None:
            title = getattr(issue, "title", None)
            if title:
                payload["title"] = title
            branch = getattr(issue, "branch_name", None)
            if branch:
                payload["branch"] = branch
            pr_url = getattr(issue, "pr_url", None)
            if pr_url:
                payload["pr"] = pr_url
        repo = self._repo_label()
        if repo:
            payload["repo"] = repo
        ver = getattr(session, "verification_status", None)
        if ver:
            payload["verification"] = ver
        # Try to get commit sha from the registry record
        issue_id = getattr(issue, "id", None) if issue is not None else None
        registry = getattr(self, "_registry", None)
        if issue_id and registry is not None:
            record = registry.get(issue_id)
            if record is not None:
                commit = getattr(record, "commit_sha", None)
                if commit:
                    payload.setdefault("commit", commit)
        payload.update({k: v for k, v in extra.items() if v is not None})
        return payload

    def _register_collaboration_modes(
        self, workflow: WorkflowConfig, agent_runner: BackendRunner
    ) -> None:
        """Register the ``ModeRunner`` instances that match ``modes.enabled``.

        ``single`` is always registered (it's the safe fallback). Other
        modes are registered only when listed in ``workflow.modes.enabled``
        so an operator can disable a mode without removing its code.
        """
        # Always register "single" — it's both the default fallback and
        # the run mode for legacy / followup / review_followup paths.
        _modes.register("single", SingleModeRunner(agent_runner))

        enabled = {m.strip().lower() for m in workflow.modes.enabled if m}
        if "pipeline" in enabled:
            stages = tuple(workflow.modes.pipeline_stages)
            max_retries = int(getattr(workflow.modes, "pipeline_max_retries_per_stage", 1))
            stage_models = dict(getattr(workflow.modes, "pipeline_stage_models", None) or {})
            stage_max_turns = dict(getattr(workflow.modes, "pipeline_stage_max_turns", None) or {})
            stage_specs = dict(getattr(workflow.modes, "pipeline_stage_specs", None) or {})
            handoff = str(getattr(workflow.modes, "pipeline_handoff", "prompt"))
            try:
                _modes.register(
                    "pipeline",
                    PipelineModeRunner(
                        agent_runner,
                        stages=stages,
                        max_retries_per_stage=max_retries,
                        stage_models=stage_models,
                        stage_max_turns=stage_max_turns,
                        stage_specs=stage_specs,
                        handoff=handoff,
                    ),
                )
            except ValueError as exc:
                # Bad stage_specs (e.g. kind=pipeline nested). Fall back
                # to a spec-less pipeline so the daemon keeps running.
                logger.warning(
                    "Pipeline registration failed (%s) — registering without stage_specs",
                    exc,
                )
                _modes.register(
                    "pipeline",
                    PipelineModeRunner(
                        agent_runner,
                        stages=stages,
                        max_retries_per_stage=max_retries,
                        stage_models=stage_models,
                        stage_max_turns=stage_max_turns,
                        stage_specs={},
                        handoff=handoff,
                    ),
                )
                stage_specs = {}
            logger.info(
                "Collaboration mode registered: pipeline (stages=%s, "
                "max_retries_per_stage=%d, stage_models=%s, "
                "stage_max_turns=%s, stage_specs=%s, handoff=%s)",
                stages,
                max_retries,
                stage_models or "(none)",
                stage_max_turns or "(none)",
                stage_specs or "(none)",
                handoff,
            )
        if "coordinator" in enabled:
            _modes.register("coordinator", CoordinatorModeRunner(agent_runner))
            logger.info("Collaboration mode registered: coordinator")
        if "swarm" in enabled:
            _modes.register(
                "swarm",
                SwarmModeRunner(
                    agent_runner,
                    max_subtasks=workflow.modes.swarm_max_subtasks,
                    max_parallel=workflow.modes.swarm_max_parallel,
                    max_waves=workflow.modes.swarm_max_waves,
                ),
            )
            logger.info(
                "Collaboration mode registered: swarm (max_subtasks=%d, "
                "max_parallel=%d, max_waves=%d)",
                workflow.modes.swarm_max_subtasks,
                workflow.modes.swarm_max_parallel,
                workflow.modes.swarm_max_waves,
            )
        if "debate" in enabled:
            proposers = tuple(
                getattr(workflow.modes, "debate_proposers", None) or ("proposer_a", "proposer_b")
            )
            judge_model = getattr(workflow.modes, "debate_judge_model", None)
            isolation = getattr(workflow.modes, "debate_isolation", "reset")
            proposer_models = dict(getattr(workflow.modes, "debate_proposer_models", None) or {})
            parallel = bool(getattr(workflow.modes, "debate_parallel", False))
            judge_mode = str(getattr(workflow.modes, "debate_judge_mode", "pick"))
            try:
                _modes.register(
                    "debate",
                    DebateModeRunner(
                        agent_runner,
                        proposers=proposers,
                        judge_model=judge_model,
                        isolation=isolation,
                        proposer_models=proposer_models,
                        parallel=parallel,
                        judge_mode=judge_mode,
                    ),
                )
            except ValueError as exc:
                # Most likely: parallel=True without isolation=worktree,
                # or an invalid judge_mode. Fall back to safe defaults so
                # the daemon keeps running.
                logger.warning(
                    "Debate registration failed (%s) — registering with "
                    "parallel=False, isolation='%s', judge_mode='pick'",
                    exc,
                    isolation,
                )
                _modes.register(
                    "debate",
                    DebateModeRunner(
                        agent_runner,
                        proposers=proposers,
                        judge_model=judge_model,
                        isolation=isolation,
                        proposer_models=proposer_models,
                        parallel=False,
                        judge_mode="pick",
                    ),
                )
                parallel = False
                judge_mode = "pick"
            logger.info(
                "Collaboration mode registered: debate (proposers=%s, "
                "judge_model=%s, isolation=%s, parallel=%s, "
                "proposer_models=%s, judge_mode=%s)",
                proposers,
                judge_model or "(default)",
                isolation,
                parallel,
                proposer_models or "(none)",
                judge_mode,
            )

    def _build_mode_selector(self, workflow: WorkflowConfig) -> ModeSelector:
        """Construct ``ModeSelector`` with the configured router backend."""
        router: Router | None
        kind = workflow.modes.router_kind
        if kind == "heuristic":
            router = HeuristicRouter()
            logger.info("ModeSelector: router=HeuristicRouter")
        elif kind == "llm":
            router = LLMRouter(
                model=workflow.modes.router_model,
                endpoint=workflow.modes.router_endpoint,
                api_key_env_var=workflow.modes.router_api_key_env,
                timeout_seconds=workflow.modes.router_timeout_seconds,
            )
            logger.info(
                "ModeSelector: router=LLMRouter(model=%s, endpoint=%s, "
                "api_key_env=%s, timeout=%.1fs)",
                workflow.modes.router_model,
                workflow.modes.router_endpoint,
                workflow.modes.router_api_key_env,
                workflow.modes.router_timeout_seconds,
            )
        else:
            router = None
            logger.info("ModeSelector: no router configured (kind=%s)", kind)

        default_mode = workflow.modes.default
        try:
            return ModeSelector(
                default_mode=default_mode,
                router=router,
                min_confidence=workflow.modes.router_min_confidence,
            )
        except ValueError as exc:
            # workflow.md misconfiguration — fall back to safe defaults
            # instead of crashing the daemon at startup.
            logger.warning("ModeSelector construction failed (%s); using defaults", exc)
            return ModeSelector()

    def _validate_workspace_strategy(self) -> None:
        if self.workflow.workspace.strategy != "sequential":
            return
        if self.workflow.agent.max_concurrent_agents != 1:
            raise ValueError("workspace.strategy=sequential requires agent.max_concurrent_agents=1")
        over_limit_states = [
            state
            for state, limit in self.workflow.agent.max_concurrent_agents_by_state.items()
            if limit > 1
        ]
        if over_limit_states:
            raise ValueError(
                "workspace.strategy=sequential requires all "
                "agent.max_concurrent_agents_by_state values to be <= 1"
            )

    def _sync_gitignore_to_workspace(self, workspace: Any) -> None:
        """Write ignore patterns for orchestrator-managed workspace files.

        Always writes to ``.git/info/exclude`` (local-only) rather than
        ``.gitignore`` so that orchestrator patterns are never tracked by
        git and never appear in agent commits.
        """
        workspace_path = Path(workspace.path)
        ignore_path = workspace_path / ".git" / "info" / "exclude"
        if not ignore_path.parent.exists():
            return

        patterns = self.git_sync._gitignore_patterns
        existing: set[str] = set()
        if ignore_path.exists():
            existing = {
                line.strip()
                for line in ignore_path.read_text(encoding="utf-8").splitlines()
                if line.strip() and not line.startswith("#")
            }

        new_patterns = [p for p in patterns if p not in existing]
        if not new_patterns:
            return

        with ignore_path.open("a", encoding="utf-8") as f:
            if ignore_path.exists() and ignore_path.stat().st_size > 0:
                f.write("\n")
            f.write("# Orchestratord managed — do not edit manually\n")
            for p in new_patterns:
                f.write(f"{p}\n")
        logger.debug("Updated %s with %d patterns", ignore_path, len(new_patterns))

    async def run(self) -> None:
        """Main polling loop. Runs until cancelled."""
        logger.info(
            "Orchestrator starting: interval=%sms max_concurrent=%s",
            self._state.poll_interval_ms,
            self._state.max_concurrent_agents,
        )

        # Best-effort session_start at the top of the polling
        # loop. The session id is the workflow root path's basename
        # plus a stable hash so the per-day aggregator can group all
        # orchestrator daemons across the day. Failures are swallowed.
        orch_start = time.monotonic()
        orch_session_id = self._derive_orchestrator_session_id()
        try:
            from orchestratord.telemetry import record_session_start

            record_session_start(
                session_id=orch_session_id,
                entrypoint="orchestrator",
                client_type="cli",
                is_non_interactive=True,
            )
        except Exception:
            pass

        # Clean up terminal workspaces on startup
        await self.workspace.run_terminal_workspace_cleanup()
        await self._recover_stale_running_records()

        # Start metadata heartbeat for CLI discovery
        heartbeat_task = asyncio.create_task(self._metadata_heartbeat_loop())
        self._tasks.add(heartbeat_task)

        try:
            while not self._shutdown_event.is_set():
                await self._poll_and_dispatch()
                try:
                    await asyncio.wait_for(
                        self._shutdown_event.wait(),
                        timeout=self._state.poll_interval_ms / 1000.0,
                    )
                except asyncio.TimeoutError:
                    pass

            logger.info("Orchestrator shutting down")
            await self._cancel_all_tasks()
            exit_status = 0
        except Exception as exc:
            # Best-effort error event with stable fingerprint.
            # Failures are swallowed.
            try:
                from orchestratord.telemetry import record_error

                record_error(session_id=orch_session_id, exc=exc)
            except Exception:
                pass
            exit_status = 1
            raise
        finally:
            # Best-effort session_end + command_run.
            try:
                from orchestratord.telemetry import (
                    record_command_run,
                    record_session_end,
                )

                duration_s = time.monotonic() - orch_start
                record_session_end(
                    session_id=orch_session_id,
                    duration_s=duration_s,
                    exit_status=exit_status,
                )
                record_command_run(
                    session_id=orch_session_id,
                    command_name="orchestrator",
                    mode="daemon",
                    success=(exit_status == 0),
                    duration_s=duration_s,
                    exit_status=exit_status,
                )
            except Exception:
                pass

    def _derive_orchestrator_session_id(self) -> str:
        """Stable session id for the orchestrator daemon.

        Combines the workspace root path with a daily salt so all
        orchestrator daemons on a given day share the same id
        (the polling loop is one continuous session for telemetry
        purposes — restart on a new day = new session).
        """
        try:
            from datetime import datetime, timezone
            import hashlib

            workspace = str(self._workspace_root) if self._workspace_root else ""
            day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            raw = f"orchestrator:{workspace}:{day}"
            return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]
        except Exception:
            return "orchestrator"

    def _report_telemetry(self) -> None:
        """Best-effort: push today's telemetry summary to the remote issue.

        Only when ``workflow.telemetry.reporting_enabled`` is set; the
        api_key falls back to the tracker's GitCode token. Never raises.
        """
        try:
            tele = getattr(self.workflow, "telemetry", None)
            if tele is None or not tele.reporting_enabled:
                return
            tracker = getattr(self.workflow, "tracker", None)
            api_key = tele.api_key or (getattr(tracker, "api_key", "") or "")
            if not api_key:
                return
            from orchestratord.telemetry.reporters import report_day

            report_day(
                owner=tele.report_owner or getattr(tracker, "owner", "") or "",
                repo=tele.report_repo or getattr(tracker, "repo", "") or "",
                api_key=api_key,
                title=tele.issue_title,
                force=True,
            )
        except Exception:
            logger.debug("telemetry report skipped", exc_info=True)

    async def _recover_stale_running_records(self) -> None:
        reason = "Recovered stale running issue on orchestrator startup"
        stale_records = self._registry.running_records()
        for record in stale_records:
            self._registry.mark_failed_with_reason(record.issue_id, reason)
            await self._sync_tracker_issue_state(record.issue_id, "failed")
            logger.warning(
                "Recovered stale running issue_id=%s on orchestrator startup",
                record.issue_id,
            )

    async def _metadata_heartbeat_loop(self) -> None:
        """Periodically rewrite metadata so CLI can always discover the orchestrator.

        If metadata.json is accidentally deleted, this recreates it within
        the heartbeat interval (30s), preventing the ``server start`` PID
        guard from being bypassed for a running instance.
        """
        from .workspace_locator import write_orchestrator_metadata

        while not self._shutdown_event.is_set():
            try:
                await asyncio.wait_for(
                    self._shutdown_event.wait(),
                    timeout=30.0,
                )
                break  # shutdown requested
            except asyncio.TimeoutError:
                pass

            write_orchestrator_metadata(
                workspace_root=self._workspace_root,
                workflow_path=self._workflow_path,
                started_at=self._metadata_started_at,
            )

    async def shutdown(self) -> None:
        """Signal graceful shutdown and clean up metadata."""
        self._shutdown_event.set()
        # Clean up orchestrator metadata
        from .workspace_locator import clear_orchestrator_metadata

        clear_orchestrator_metadata(self._workspace_root)

    def _workflow_mtime_ns(self) -> int | None:
        """Return the workflow modification time, if this daemon has a file."""
        if not self._workflow_path:
            return None
        try:
            return Path(self._workflow_path).stat().st_mtime_ns
        except OSError:
            return None

    def _refresh_dynamic_title_prefix_filter(self) -> None:
        """Apply changed title-prefix settings before fetching candidates.

        Only this lightweight tracker setting is hot-reloaded.  Other workflow
        settings can affect running work and retain the daemon-start snapshot.
        """
        mtime_ns = self._workflow_mtime_ns()
        if mtime_ns is None or mtime_ns == self._dynamic_tracker_config_mtime_ns:
            return
        # Record first so a malformed edit does not generate a warning every
        # polling interval. A subsequent file save will be retried.
        self._dynamic_tracker_config_mtime_ns = mtime_ns
        try:
            from .workflow import WorkflowLoader

            refreshed, _ = WorkflowLoader.load(self._workflow_path or "")
        except Exception as exc:
            logger.warning("workflow title-prefix reload failed: %s", exc)
            return

        if refreshed.tracker.kind != self.workflow.tracker.kind:
            logger.warning(
                "workflow tracker kind changed from %s to %s; title-prefix reload ignored",
                self.workflow.tracker.kind,
                refreshed.tracker.kind,
            )
            return
        configure = getattr(self.tracker, "configure_title_prefix_filter", None)
        if not callable(configure):
            logger.warning("tracker does not support dynamic title-prefix filtering")
            return
        configure(
            refreshed.tracker.title_prefixes,
            refreshed.tracker.title_prefix_match,
        )
        self.workflow.tracker.title_prefixes = refreshed.tracker.title_prefixes
        self.workflow.tracker.title_prefix_match = refreshed.tracker.title_prefix_match
        logger.info(
            "reloaded title-prefix filter: mode=%s prefixes=%s",
            refreshed.tracker.title_prefix_match,
            refreshed.tracker.title_prefixes,
        )

    async def _poll_and_dispatch(self) -> None:
        """Fetch candidates, respect concurrency limit, launch runs."""
        self.status_dashboard.on_poll_start()
        self._state.poll_check_in_progress = True

        try:
            self._refresh_dynamic_title_prefix_filter()
            # Process lifecycle control commands (pause/resume/stop/takeover)
            await self._process_control_commands()

            # Poll clarification answers (Channel 2 + Channel 3)
            await self._clarification_resolver.poll_clarification_answers()

            # Process retry queue first
            await self._process_retry_queue()

            # Handle escalated (clarification-exhausted) issues
            await self._process_escalated_issues()

            await self._process_review_feedback()

            # Launch agent_rebase for PRs with content conflicts
            await self._process_pending_rebase_conflicts()
            # Optional PR mergeable-state scan (opt-in via workflow.md)
            await self._process_pr_conflict_scan()

            # Fetch new candidate issues
            try:
                issues = await self.tracker.fetch_candidate_issues()
            except Exception as exc:
                logger.error("Failed to fetch candidate issues: %s", exc)
                return

            available_slots = self._state.max_concurrent_agents - len(self._state.running)
            if self._clarification_gate is not None:
                self._clarification_gate.begin_poll()

            # Pre-register all unregistered candidates with QUEUED status
            # so the dashboard / registry reflects the full backlog.
            for issue in issues:
                if not self._registry.get(issue.id or ""):
                    base_branch = (
                        getattr(issue, "base_branch", None)
                        or self.workflow.workspace.base_branch
                        or "main"
                    )
                    self._registry.register(
                        issue_id=issue.id or "",
                        issue_identifier=issue.identifier or "",
                        branch_name=issue.branch_name,
                        base_branch=base_branch,
                        status=IssueStatus.QUEUED,
                        author_login=issue.author_login,
                    )
                    # Notify the operator that a new issue was discovered.
                    # The Issue object (with url, title, identifier) is
                    # directly in scope here — all tracker adapters
                    # populate issue.url from the platform API response.
                    self._emit_im_event(
                        issue.id or "",
                        "issue.detected",
                        EventLevel.INFO,
                        "新增 ISSUE",
                        self._issue_payload(issue, url=issue.url),
                    )
                elif issue.author_login:
                    record = self._registry.get(issue.id or "")
                    if record is not None and not record.author_login:
                        record.author_login = issue.author_login
                        self._registry._save()

            if self.workflow.workspace.strategy == "sequential" and self._state.running:
                return

            launched_this_poll = 0
            for issue in issues:
                if launched_this_poll >= available_slots:
                    break
                if issue.id in self._state.running:
                    continue
                if issue.id in self._state.claimed:
                    continue

                # Intent resolution must
                # happen BEFORE the completed/pending_review skip so
                # operators can trigger RETRY / FOLLOWUP on completed
                # issues via labels, comments, or CLI.
                intent, command_intent_obj, intent_source = await self._resolve_intent(issue)

                if issue.id in self._state.completed or issue.id in self._state.pending_review:
                    if intent not in (Intent.RETRY, Intent.FOLLOWUP):
                        continue
                # `command_intent_obj` may carry the comment author
                # for role checks; the bare `Command` value
                # is in `command_intent_obj.command`.
                command = command_intent_obj.command if command_intent_obj is not None else None
                command_author = (
                    command_intent_obj.author_login if command_intent_obj is not None else None
                )

                # Role check. If a comment command is
                # what triggered the intent, only the issue author or
                # a maintainer (or `allow_anyone_to_retry=True`) is
                # allowed to fire it. The check happens BEFORE the
                # acknowledgement comment is posted, so a rejected
                # command never advances the cursor.
                if (
                    command_intent_obj is not None
                    and intent in (Intent.RETRY, Intent.FOLLOWUP)
                    and not self._is_command_author_eligible(issue, command_author)
                ):
                    await self._reject_unauthorized_command(issue, command_intent_obj)
                    continue

                # Rate limit on RETRY intent. If the issue
                # has hit `max_retries_per_issue`, refuse the reset
                # (even with `--force`; only the label-based retry
                # honors force in the daemon path).
                if intent is Intent.RETRY:
                    if not self._check_retry_rate_limit(issue, force=False):
                        continue

                # When a comment command is honored, post
                # a bot acknowledgement so the operator sees the
                # intent was received, and record the command on the
                # registry for audit.
                if command is not None:
                    await self._post_command_acknowledgement(issue, command)
                    record = self._registry.get(issue.id or "")
                    if record is not None:
                        record.last_command = f"/agent {command.value}"
                        record.touch()
                        self._registry._save()
                    logger.info(
                        "Issue %s command received: /agent %s",
                        issue.id,
                        command.value,
                    )

                    # UNBLOCK is a meta-command: clear any BLOCKED
                    # state so the next poll re-applies the (now
                    # possibly cleared) label-based intent.
                    if command is Command.UNBLOCK:
                        record = self._registry.get(issue.id or "")
                        if record is not None and record.status is IssueStatus.ABANDONED:
                            logger.info(
                                "Issue %s unblocked, status reset to pending",
                                issue.id,
                            )
                            record.status = IssueStatus.PENDING
                            record.intent = Intent.NONE
                            record.intent_source = None
                            self._registry._save()

                if intent is Intent.BLOCKED:
                    logger.info(
                        "Issue %s blocked intent detected, marking abandoned",
                        issue.id,
                    )
                    record = self._registry.get(issue.id or "")
                    if record is None:
                        self._registry.register(
                            issue_id=issue.id or "",
                            issue_identifier=issue.identifier or "",
                            branch_name=getattr(issue, "branch_name", None) or "main",
                        )
                    self._registry.mark_intent(
                        issue.id or "",
                        intent,
                        # Preserve the source from
                        # _resolve_intent so CLI / comment / label
                        # origin is recorded on the record. The
                        # fallback only fires if intent_source is
                        # somehow None (defensive — should not be
                        # reachable when intent is RETRY/FOLLOWUP/
                        # BLOCKED).
                        source=(intent_source or ("command" if command is not None else "label")),
                        command=(f"/agent {command.value}" if command is not None else None),
                    )
                    self._registry.mark_abandoned(issue.id or "")
                    await self._sync_tracker_issue_state(issue.id or "", "abandoned")
                    self._state.completed.add(issue.id or "")
                    continue

                if intent is Intent.RETRY:
                    logger.info(
                        "Issue %s retry intent detected, will reset on launch",
                        issue.id,
                    )
                    self._registry.mark_intent(
                        issue.id or "",
                        intent,
                        # Preserve the source from
                        # _resolve_intent so CLI / comment / label
                        # origin is recorded on the record. The
                        # fallback only fires if intent_source is
                        # somehow None (defensive — should not be
                        # reachable when intent is RETRY/FOLLOWUP/
                        # BLOCKED).
                        source=(intent_source or ("command" if command is not None else "label")),
                        command=(f"/agent {command.value}" if command is not None else None),
                    )
                    # The reset+close path performs the actual reset.
                elif intent is Intent.FOLLOWUP:
                    logger.info(
                        "Issue %s follow-up intent detected, will reuse branch",
                        issue.id,
                    )
                    self._registry.mark_intent(
                        issue.id or "",
                        intent,
                        # Preserve the source from
                        # _resolve_intent so CLI / comment / label
                        # origin is recorded on the record. The
                        # fallback only fires if intent_source is
                        # somehow None (defensive — should not be
                        # reachable when intent is RETRY/FOLLOWUP/
                        # BLOCKED).
                        source=(intent_source or ("command" if command is not None else "label")),
                        command=(f"/agent {command.value}" if command is not None else None),
                    )
                    # /agent follow-up = 检视意见处理的重试：拉取 PR 上未处理的
                    # 检视（pending）并让 agent 处理——而不是退回"完整 issue
                    # 任务重跑"（agent_followup 的 fresh issue-style run）。
                    followup_handled = await self._launch_followup_with_pending_reviews(
                        issue
                    )
                    if followup_handled:
                        continue
                    # 无待处理检视（或无法采集）——不启动完整任务重跑。
                    logger.info(
                        "Issue %s follow-up: no pending review feedback to process — skip",
                        issue.id,
                    )
                    continue

                if intent is Intent.REBASE:
                    # REBASE intent — the orchestrator itself
                    # performs the rebase (no agent for clean rebases).
                    # On content conflict, has_conflict is set and the
                    # next ``_process_pending_rebase_conflicts`` cycle
                    # launches an agent_rebase run.
                    logger.info(
                        "Issue %s rebase intent detected, running built-in rebase",
                        issue.id,
                    )
                    self._registry.mark_intent(
                        issue.id or "",
                        intent,
                        source=(intent_source or ("command" if command is not None else "label")),
                        command=(f"/agent {command.value}" if command is not None else None),
                    )
                    if not self._check_rebase_rate_limit(issue, force=False):
                        continue
                    await self._process_rebase_intent(issue)
                    # CLI is one-shot; clear so the next poll doesn't
                    # re-trigger. Audit + last_command are preserved.
                    if intent_source == "cli":
                        self._registry.clear_intent(issue.id or "")
                    continue

                # Skip terminal registry records even if the tracker still
                # exposes the issue in an active state. Explicit retry/follow-up
                # intents are the only daemon path that may reopen handled work.
                if intent is Intent.NONE and (
                    self._registry.is_terminal(issue.id or "")
                    or self._registry.has_pr(issue.id or "")
                ):
                    logger.info("Issue %s already handled (registry), skipping", issue.id)
                    continue
                if not await self._dependencies_satisfied(issue):
                    continue
                if self._clarification_gate is not None:
                    try:
                        if not await self._clarification_gate.should_dispatch(issue):
                            logger.info("Issue %s is waiting for issue-clarifier clarification", issue.id)
                            continue
                    except Exception:
                        logger.exception("Issue-clarifier clarity gate failed for issue %s", issue.id)
                        if not bool(getattr(self.workflow.clarifier, "fail_open", True)):
                            continue
                self._state.claimed.add(issue.id)
                # Thread-local MDC for the orchestrator launch path —
                # the agent_runner will refill with run_id once available.
                from .logging_setup import set_log_context

                set_log_context(
                    issue_id=str(issue.id or ""),
                    issue_identifier=str(getattr(issue, "identifier", "")),
                )
                await self._launch_issue(issue)
                if issue.id in self._state.running:
                    launched_this_poll += 1
                    # CLI retry is a one-shot. The
                    # operator's orchestrator issue command
                    # retry --mode reset` already wrote `registry.intent`
                    # with `intent_source="cli"`; now that the launch
                    # has started, clear it so the next poll does NOT
                    # re-trigger. The audit trail (the original
                    # `last_command` text + the high-priority audit
                    # log entry written by the CLI) is preserved.
                    if intent_source == "cli":
                        self._registry.clear_intent(issue.id or "")

        finally:
            self._state.poll_check_in_progress = False
            self.status_dashboard.on_poll_end()
            # Poll 结束后广播澄清状态到 dashboard
            self._broadcast_clarification_status()

    async def _dependencies_satisfied(self, issue: Issue) -> bool:
        dependencies = [dep for dep in getattr(issue, "depends_on", []) if dep]
        if not dependencies:
            return True

        unresolved = [
            dependency
            for dependency in dependencies
            if not (self._registry.is_completed(dependency) or self._registry.has_pr(dependency))
        ]
        if unresolved:
            logger.info(
                "Issue %s waiting for dependencies: %s",
                issue.id,
                ", ".join(unresolved),
            )
            return False
        return True

    async def _resolve_intent(
        self,
        issue: Issue,
    ) -> tuple[Intent, "CommandIntent | None", str | None]:
        """Resolve the current operator intent for an issue.

        Merges three intent sources:
          1. Label-based intent (Sub-A: `agent:retry` / `agent:follow-up`
             / `agent:blocked`).
          2. Comment-based command (Sub-D: `/agent retry` / `/agent
             follow-up` / `/agent unblock`).
          3. Registry-based CLI intent (Sub-E: orchestrator
             orchestrator issue retry --mode reset|followup|unblock`
             writes `registry.intent` with `intent_source="cli"`).

        Priority (high → low): BLOCKED is sticky; CLI beats comment
        beats label. CLI is the operator's authoritative local command
        and must survive even when the remote issue tracker is
        unreachable / read-only / local-only (LocalTracker).

        Returns ``(intent, command_intent_obj, intent_source)``:
          * ``intent`` — merged Intent for the launch.
          * ``command_intent_obj`` — the raw CommandIntent (with the
            comment's author login for the role check) if a
            comment command was honored, else None.
          * ``intent_source`` — the source that won the merge
            (``"cli"`` | ``"command"`` | ``"label"`` | None) so the
            caller can preserve the audit trail in `mark_intent` and
            decide whether to clear the intent after launch.
        """
        labels = list(getattr(issue, "labels", None) or [])
        label_intent = Intent.NONE
        if labels:
            try:
                label_intent = await self.tracker.extract_intent_from_labels(labels)
            except Exception as exc:
                logger.warning(
                    "Failed to extract intent from labels for issue %s: %s",
                    issue.id,
                    exc,
                )

        # Comment command intent.
        command_intent_obj = await self._resolve_command_intent(issue)
        command = command_intent_obj.command if command_intent_obj is not None else None
        command_intent = command_to_intent(command) if command is not None else Intent.NONE

        # CLI fallback intent. The CLI is the operator's
        # authoritative local command, so we read it directly from
        # `registry.intent` whenever the record carries
        # `intent_source="cli"`. The CLI path does NOT require the
        # remote issue tracker to be reachable, so this is also the
        # only intent source that works for LocalTracker users and
        # for operators working offline.
        cli_intent = Intent.NONE
        record = self._registry.get(issue.id or "")
        if record is not None and getattr(record, "intent_source", None) == "cli":
            raw_intent = getattr(record, "intent", None)
            if raw_intent:
                try:
                    cli_intent = Intent(raw_intent)
                except ValueError:
                    logger.warning(
                        "Issue %s has unknown CLI intent %r, ignoring",
                        issue.id,
                        raw_intent,
                    )
                    cli_intent = Intent.NONE

        merged = merge_intents_with_cli(label_intent, command_intent, cli_intent)

        # Track which source won so downstream `mark_intent` calls
        # preserve the audit trail. The order matches the merge
        # priority (BLOCKED > CLI > command > label).
        intent_source: str | None = None
        if merged is Intent.BLOCKED:
            if label_intent is Intent.BLOCKED:
                intent_source = "label"
            elif command_intent is Intent.BLOCKED:
                intent_source = "command"
            elif cli_intent is Intent.BLOCKED:
                intent_source = "cli"
        elif cli_intent is not Intent.NONE and merged is cli_intent:
            intent_source = "cli"
        elif command_intent is not Intent.NONE and merged is command_intent:
            intent_source = "command"
        elif label_intent is not Intent.NONE and merged is label_intent:
            intent_source = "label"

        return merged, command_intent_obj, intent_source

    async def _resolve_command_intent(self, issue: Issue) -> "CommandIntent | None":
        """Fetch and parse the most recent /agent command.

        The returned `CommandIntent` carries the comment
        author so the caller can perform the role check. Adapters that
        don't expose author info will return `author_login=None`, in
        which case `_is_command_author_eligible` will reject the
        command (fail-closed) to avoid the LLM-self-trigger risk.
        """
        issue_id = issue.id or ""
        if not issue_id:
            return None
        record = self._registry.get(issue_id)
        cursor = record.command_cursor if record is not None else None
        if not supports(self.tracker, CommandIntentCapability):
            return None
        try:
            return await self.tracker.fetch_issue_command_intent(issue_id, cursor)
        except Exception as exc:
            logger.warning(
                "Failed to fetch issue command intent for %s: %s",
                issue_id,
                exc,
            )
            return None

    async def _post_command_acknowledgement(
        self,
        issue: Issue,
        command: "Command",
    ) -> str | None:
        """Post a bot confirmation comment and update cursor.

        The confirmation comment includes a metadata HTML comment
        with `command_cursor` so the next poll knows where to resume
        scanning. Returns the created comment ID, or None on
        failure.
        """
        issue_id = issue.id or ""
        body = f"## Orchestratord: 已受理 /agent {command.value}\n\n下一轮 poll 开始执行。\n"
        try:
            comment = await self.tracker.create_comment(issue_id, body)
        except Exception as exc:
            logger.warning(
                "Failed to post command acknowledgement for %s: %s",
                issue_id,
                exc,
            )
            return None
        comment_id = getattr(comment, "id", None) if comment is not None else None
        if comment_id:
            record = self._registry.get(issue_id)
            if record is not None:
                record.command_cursor = comment_id
                self._registry._save()
        return comment_id

    # ------------------------------------------------------------------
    # Role check + rate-limit guard
    # ------------------------------------------------------------------

    def _is_command_author_eligible(
        self,
        issue: Issue,
        author_login: str | None,
    ) -> bool:
        """Return True if `author_login` may trigger a retry/follow-up.

        Only the issue author may trigger. The maintainer role is not
        consulted: deploy tokens have heterogeneous permission levels and
        the platform collaborators API is not reliably reachable with a
        regular token, so a maintainer check cannot be certified
        uniformly. Keep the check fail-closed on the issue author.

        Short-circuits:

          1. `workflow.agent.allow_anyone_to_retry` — disables the
             role check entirely (trusted-team mode).
          2. `author_login` is None — fail-closed. Adapters that
             don't expose author info cannot pass the check; this
             prevents the LLM-self-trigger risk where a bot
             accidentally writes `/agent retry` in its own reply
             and the daemon can't tell it wasn't a human.
          3. The bot itself (`orchestratord`) is always allowed so the
             CLI fallback (`/agent retry` from a local operator
             routed through the bot) isn't rejected. NOTE: the CLI
             path doesn't actually go through this code path; this
             branch is only here to be lenient on platform quirks
             where the bot appears as the author of its own ack
             comment.
        """
        if getattr(self.workflow.agent, "allow_anyone_to_retry", False):
            return True
        if not author_login:
            # Fail-closed: if we don't know who wrote the command,
            # we cannot certify they are not the LLM itself.
            return False
        if author_login == "orchestratord":
            return True
        record = self._registry.get(issue.id or "")
        issue_author = getattr(record, "author_login", None) if record else None
        return bool(issue_author and author_login == issue_author)

    async def _reject_unauthorized_command(
        self,
        issue: Issue,
        command_intent: "CommandIntent",
    ) -> None:
        """Post a comment rejecting an unauthorized command.

        Per the design acceptance criteria: "用户在 issue comment 发
        `/agent retry`,且非原作者时,**daemon 拒绝执行**并发评论
        `## Orchestratord: 仅 issue 作者或 maintainer 可触发 /agent retry`".
        """
        issue_id = issue.id or ""
        body = (
            f"## Orchestratord: 仅 issue 作者或 maintainer 可触发 "
            f"/agent {command_intent.command.value}\n\n"
            f"author=`{command_intent.author_login or '<unknown>'}` "
            f"not authorized; ignored.\n"
        )
        try:
            await self.tracker.create_comment(issue_id, body)
        except Exception as exc:
            logger.warning(
                "Failed to post unauthorized-command rejection for %s: %s",
                issue_id,
                exc,
            )
        # Advance the command cursor past this rejected comment so the
        # next poll does not re-scan it and re-print the rejection.
        # Without this, an unauthorized comment is re-processed every
        # poll until a human deletes it.
        rejected_comment_id = command_intent.comment_id
        if rejected_comment_id:
            record = self._registry.get(issue_id)
            if record is not None:
                record.command_cursor = rejected_comment_id
                self._registry._save()
        logger.info(
            "Issue %s command rejected: /agent %s by %s (not authorized)",
            issue_id,
            command_intent.command.value,
            command_intent.author_login,
        )
        self._log_audit_event(
            issue_id=issue_id,
            event="unauthorized_command",
            mode=f"command:{command_intent.command.value}",
            reason="role_check_failed",
            author=command_intent.author_login or "unknown",
        )

    def _check_retry_rate_limit(
        self,
        issue: Issue,
        *,
        force: bool = False,
    ) -> bool:
        """Refuse a RETRY when retry_count >= max_retries_per_issue.

        Returns True if the retry is allowed (and bumps
        `retry_count` for the record), or False if the rate limit
        was hit. The caller is responsible for the actual reset
        work; this helper is a guard.

        On a hit, this method:
          * Logs the rejection.
          * Appends an `agent:retry-rejected` label to the issue
            (best-effort).
          * Posts a comment explaining the rejection.
          * Records a high-priority audit.jsonl entry.
        """
        issue_id = issue.id or ""
        max_retries = getattr(self.workflow.agent, "max_retries_per_issue", 3)
        record = self._registry.get(issue_id)
        current = record.retry_count if record else 0
        if current < max_retries:
            return True
        if force:
            # `force=True` is reserved for the CLI path, which
            # logs its own audit entry. The daemon path passes
            # `force=False` and is therefore rejected on the
            # `current >= max_retries` branch.
            return True
        # Rate limit hit; do the side-effects.
        logger.warning(
            "Issue %s retry rate limit hit: %d >= %d",
            issue_id,
            current,
            max_retries,
        )
        self._log_audit_event(
            issue_id=issue_id,
            event="retry_rejected",
            mode="label:agent:retry",
            reason=f"retry_count={current} >= max_retries_per_issue={max_retries}",
            author="daemon",
        )
        # Best-effort: add the agent:retry-rejected label and
        # post a comment. Failures here are logged but do not
        # change the verdict (False = reject).
        import asyncio

        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            loop = None
        if loop is not None and loop.is_running():
            asyncio.create_task(self._post_retry_rejection(issue_id, current, max_retries))
        else:
            asyncio.run(self._post_retry_rejection(issue_id, current, max_retries))
        return False

    async def _post_retry_rejection(
        self,
        issue_id: str,
        current: int,
        max_retries: int,
    ) -> None:
        """Best-effort label + comment for rate-limit hits."""
        body = (
            f"## Orchestratord: retry rate limit reached\n\n"
            f"This issue has been retried {current} times "
            f"(limit: {max_retries}). The `agent:retry` label "
            f"is being ignored. Please review manually and "
            f"either remove the label or use "
            f"`orchestratord issue retry --id {issue_id} "
            f"--mode reset --force` to bypass.\n"
        )
        try:
            await self.tracker.create_comment(issue_id, body)
        except Exception as exc:
            logger.warning(
                "Failed to post retry-rejection comment for %s: %s",
                issue_id,
                exc,
            )
        # Adding the rejection label is platform-specific. We use
        # `update_issue_state` as a no-op state-setter and try to
        # pass the label through the same channel; the adapter
        # implementations that support labels will route it.
        try:
            update_labels = getattr(self.tracker, "add_label", None)
            if callable(update_labels):
                result = update_labels(issue_id, "agent:retry-rejected")
                if hasattr(result, "__await__"):
                    await result
        except Exception as exc:
            logger.warning(
                "Failed to add agent:retry-rejected label to %s: %s",
                issue_id,
                exc,
            )

    def _log_audit_event(
        self,
        *,
        issue_id: str,
        event: str,
        mode: str,
        reason: str,
        author: str,
    ) -> None:
        """Write a daemon-side audit log entry.

        Best-effort: writes to `~/.orchestratord/orchestrator/audit.jsonl`
        (the same file the CLI uses). Failure to write is logged
        but does not affect the orchestrator's main loop.
        """
        try:
            import json
            import time
            from pathlib import Path

            log_path = AUDIT_LOG
            payload = {
                "ts": time.time(),
                "ts_iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "operator": author,
                "issue_id": issue_id,
                "mode": mode,
                "reason": reason,
                "event": event,
                "force": False,
                "priority": "high",
            }
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with log_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
        except Exception as exc:
            logger.warning(
                "Failed to write daemon audit log: %s",
                exc,
            )

    async def _prepare_intent_reset(self, issue: Issue) -> None:
        """Apply registry-side reset before launching an issue.

        Reads the persisted intent from the registry (set in
        `_poll_and_dispatch`) and, when intent == RETRY:
          1. Closes the existing remote PR (best-effort; failure is
             logged but does not block the reset).
          2. Calls `reset_for_retry(issue_id)` to clear local
             commit_sha / pr_number / pr_url / report_path / status.

        For Intent.FOLLOWUP, no reset is performed here — Sub-C will
        handle the follow-up commit path inside git_sync.sync().

        For Intent.NONE / Intent.BLOCKED, this is a no-op. The
        BLOCKED case never reaches `_launch_issue` because
        `_poll_and_dispatch` skips it.
        """
        issue_id = issue.id or ""
        if not issue_id:
            return
        record = self._registry.get(issue_id)
        if record is None:
            return
        intent = record.intent
        if intent is not Intent.RETRY:
            return

        # 1. Close the existing PR (best-effort).
        pr_number = record.pr_number
        pr_url = record.pr_url
        if pr_number:
            pr_ref = PullRequestRef(number=pr_number, url=pr_url)
            try:
                closed = await self.tracker.close_pull_request(pr_ref)
                if closed:
                    logger.info(
                        "Issue %s retry: closed remote PR %s",
                        issue_id,
                        pr_number,
                    )
                else:
                    logger.warning(
                        "Issue %s retry: tracker could not close PR %s; "
                        "continuing with local reset",
                        issue_id,
                        pr_number,
                    )
            except Exception as exc:
                logger.warning(
                    "Issue %s retry: close_pull_request raised %s; continuing with local reset",
                    issue_id,
                    exc,
                )

        # 2. Reset the local registry entry. retry_count is bumped
        # inside reset_for_retry by default.
        self._registry.reset_for_retry(issue_id)
        logger.info(
            "Issue %s retry: registry reset (attempt %d)",
            issue_id,
            (self._registry.get(issue_id) or record).retry_count,
        )

    # ------------------------------------------------------------------
    # PR Conflict Auto-Resolution
    # ------------------------------------------------------------------

    def _check_rebase_rate_limit(
        self,
        issue: Issue,
        *,
        force: bool = False,
    ) -> bool:
        """Refuse a rebase when rebase_attempt_count exceeds the cap.

        Returns True if the rebase is allowed (and bumps the
        counter on the registry record), or False if the rate
        limit was hit. ``force=True`` is reserved for the CLI path;
        the daemon path passes ``force=False`` so a hit produces a
        ``rebase_rejected`` audit entry instead of silently
        swallowing the request.
        """
        issue_id = issue.id or ""
        limit = self.workflow.pr_conflict_scan.max_rebase_attempts_per_issue
        record = self._registry.get(issue_id)
        current = record.rebase_attempt_count if record else 0
        if current < limit:
            if record is not None:
                self._registry.increment_rebase_attempt(issue_id)
            return True
        if force:
            return True
        logger.warning(
            "Issue %s rebase rate limit hit: %d >= %d",
            issue_id,
            current,
            limit,
        )
        self._log_audit_event(
            issue_id=issue_id,
            event="rebase_rejected",
            mode="rebase",
            reason=(f"rebase_attempt_count={current} >= max_rebase_attempts_per_issue={limit}"),
            author="daemon",
        )
        return False

    async def _process_rebase_intent(
        self,
        issue: Issue,
        *,
        force: bool | None = None,
    ) -> PRRebaseResult | None:
        """The built-in non-agent rebase path.

        Direct ``asyncio.to_thread(rebase_for_pr, ...)`` — no
        agent / session / provider involved. On a clean rebase
        the registry is cleared and ``rebase_completed`` is
        audited; on a content conflict ``has_conflict`` is set
        and ``rebase_conflict`` is audited (the daemon will pick
        it up in the next ``_process_pending_rebase_conflicts``
        cycle and launch an ``agent_rebase`` run).
        """
        issue_id = issue.id or ""
        record = self._registry.get(issue_id)
        if record is None:
            logger.warning(
                "Issue %s rebase skipped: registry record missing",
                issue_id,
            )
            return None
        if not record.workspace_path or not record.branch_name:
            logger.warning(
                "Issue %s rebase skipped: workspace_path=%r branch_name=%r",
                issue_id,
                record.workspace_path,
                record.branch_name,
            )
            return None
        base_branch = record.base_branch or self.workflow.workspace.base_branch or "main"
        use_force = self.workflow.pr_conflict_scan.use_force_push if force is None else force
        result = await asyncio.to_thread(
            rebase_for_pr,
            workspace_path=record.workspace_path,
            branch_name=record.branch_name,
            base_branch=base_branch,
            force=use_force,
        )
        if result.has_conflict:
            self._registry.mark_conflict(issue_id, result.conflict_files)
            # When the operator used --force, reset the rebase attempt
            # counter so _process_pending_rebase_conflicts can launch
            # the conflict-resolution agent on the next poll cycle.
            if use_force and record is not None:
                record.rebase_attempt_count = 0
                self._registry._save()
            logger.warning(
                "Issue %s rebase left conflicts: %s",
                issue_id,
                ", ".join(result.conflict_files),
            )
            self._log_audit_event(
                issue_id=issue_id,
                event="rebase_conflict",
                mode="force" if use_force else "force-with-lease",
                reason=",".join(result.conflict_files),
                author="daemon",
            )
            return result
        if result.rebased:
            self._registry.clear_conflict(issue_id)
            if result.new_head_sha:
                record.commit_sha = result.new_head_sha
                record.touch()
                self._registry._save()
            logger.info(
                "Issue %s rebase completed pushed=%s method=%s head=%s",
                issue_id,
                result.pushed,
                result.push_method,
                result.new_head_sha,
            )
            self._log_audit_event(
                issue_id=issue_id,
                event="rebase_completed",
                mode=result.push_method,
                reason="pushed" if result.pushed else "already_up_to_date",
                author="daemon",
            )
        else:
            logger.warning("Issue %s rebase did not complete", issue_id)
            self._log_audit_event(
                issue_id=issue_id,
                event="rebase_failed",
                mode="force" if use_force else "force-with-lease",
                reason="git_rebase_or_push_failed",
                author="daemon",
            )
        return result

    async def _process_pending_rebase_conflicts(self) -> None:
        """Launch ``agent_rebase`` for records with content conflicts.

        Iterates the registry, picks records with ``has_conflict=True``
        that are not already running/claimed and not rate-limited, and
        invokes ``_launch_rebase_resolution``. Each resolution opens a
        fresh ``AgentSession`` whose prompt is built by
        ``PromptBuilder.render_rebase``.
        """
        available_slots = self._state.max_concurrent_agents - len(self._state.running)
        if available_slots <= 0:
            logger.debug("No concurrency slots for rebase-resolution")
            return

        records_snapshot = list(self._registry._records.values())
        for record in records_snapshot:
            issue_id = record.issue_id or ""
            if not issue_id:
                continue
            if issue_id in self._state.running or issue_id in self._state.claimed:
                continue
            if not record.has_conflict:
                continue
            if not self._check_rebase_rate_limit(
                Issue(id=issue_id, identifier=record.issue_identifier)
            ):
                continue
            issue = await self.tracker.fetch_issue_states_by_ids([issue_id])
            issue_obj = issue.get(issue_id) if issue else None
            if issue_obj is None:
                issue_obj = Issue(
                    id=issue_id,
                    identifier=record.issue_identifier,
                    title="(unknown)",
                    branch_name=record.branch_name,
                )
            try:
                ws = await self.workspace.create_for_issue(issue_obj)
                ws_path = getattr(ws, "path", None) or record.workspace_path
                if ws_path and not record.workspace_path:
                    record.workspace_path = str(ws_path)
                    self._registry._save()
            except Exception as exc:
                logger.warning(
                    "Issue %s rebase-resolution: workspace create failed %s; using record.workspace_path",
                    issue_id,
                    exc,
                )
            await self._launch_rebase_resolution(issue_obj)

    async def _process_pr_conflict_scan(self) -> None:
        """Optional daemon scan of PR mergeable state.

        Default-disabled (opt-in via ``workflow.pr_conflict_scan.enabled``).
        When enabled, polls each open PR in the registry, asks the
        tracker for mergeability, and triggers ``_process_rebase_intent``
        if conflicts are reported.

        GitCode fallback: ``MergeableStatus(mergeable=None, has_conflicts=False)``
        is silently skipped — operators on GitCode must use CLI / label /
        comment triggers.
        """
        cfg = self.workflow.pr_conflict_scan
        if not cfg.enabled:
            return
        now = time.monotonic()
        interval_s = cfg.poll_interval_ms / 1000.0
        last_run = self._state.pr_conflict_scan_last_run
        if last_run > 0 and now - last_run < interval_s:
            return
        self._state.pr_conflict_scan_last_run = now

        for record in list(self._registry._records.values()):
            issue_id = record.issue_id or ""
            if not issue_id or not record.pr_number or not record.branch_name:
                continue
            pr_state = getattr(record, "pr_state", None)
            if pr_state and pr_state not in cfg.scan_states:
                continue
            if not self._check_rebase_rate_limit(
                Issue(id=issue_id, identifier=record.issue_identifier)
            ):
                continue
            pr_ref = PullRequestRef(
                number=record.pr_number,
                url=record.pr_url,
            )
            if not supports(self.tracker, PullRequestMaintenanceCapability):
                continue
            try:
                status = await self.tracker.fetch_pull_request_mergeable(pr_ref)
            except Exception as exc:
                logger.warning(
                    "PR conflict scan: tracker fetch failed for %s: %s",
                    issue_id,
                    exc,
                )
                continue
            if status is None or not status.has_conflicts:
                continue
            issue = await self.tracker.fetch_issue_states_by_ids([issue_id])
            issue_obj = issue.get(issue_id) if issue else None
            if issue_obj is None:
                issue_obj = Issue(
                    id=issue_id,
                    identifier=record.issue_identifier,
                    title="(unknown)",
                    branch_name=record.branch_name,
                )
            await self._process_rebase_intent(issue_obj)

    async def _launch_rebase_resolution(self, issue: Issue) -> None:
        """Launch an ``agent_rebase`` session to resolve a content conflict.

        Mirrors ``_launch_issue`` for the conflict-resolution path.
        The session is tagged with ``run_kind="agent_rebase"`` so the
        agent runner can route the run through a rebase-tailored
        prompt and dispatch policy.
        """
        record = self._registry.get(issue.id or "")
        workspace_path = record.workspace_path if record else None
        # Synthesize a minimal Workspace stub when no real workspace
        # is available; the agent runner only needs ``workspace.path``
        # to be present for prompt injection.
        if workspace_path:
            from pathlib import Path as _Path

            from .workspace import Workspace as _Ws

            workspace = _Ws(path=_Path(workspace_path), issue_identifier=issue.identifier or "")
        else:
            from pathlib import Path as _Path

            from .workspace import Workspace as _Ws

            workspace = _Ws(path=_Path("/tmp"), issue_identifier=issue.identifier or "")
        session = AgentSession(
            issue=issue,
            workspace=workspace,
            conversation_id=record.conversation_id if record is not None else None,
            parent_run_id=record.run_id if record is not None else None,
            pause_resume_event=asyncio.Event(),
            event_queue=asyncio.Queue(),
        )
        clarification_record = self._registry.get(issue.id or "")
        if clarification_record is not None and clarification_record.local_answer:
            session.clarification_answer = clarification_record.local_answer
            session.clarification_source = clarification_record.local_answer_source
            if clarification_record.question_history:
                session.clarification_question = "\n".join(
                    f"- {question}" for question in clarification_record.question_history
                )
        session.run_kind = "agent_rebase"
        # Route the run through the purpose-built rebase prompt
        # (resolve markers -> git add -> git rebase --continue ->
        # --force-with-lease push, "do NOT open a new PR"). Without this
        # the session ran the generic issue prompt and the agent never
        # knew it was supposed to resolve the rebase conflict.
        rebase_branch = (record.branch_name if record else None) or issue.branch_name or ""
        rebase_base = (
            (record.base_branch if record else None)
            or self.workflow.workspace.base_branch
            or "main"
        )
        rebase_conflicts = tuple(record.conflict_files) if record else ()
        session.prompt_override = PromptBuilder.render_rebase(
            issue=issue,
            branch_name=rebase_branch,
            base_branch=rebase_base,
            conflict_files=rebase_conflicts,
        )
        self._prepare_rebase_session(session)
        self._state.running[issue.id or ""] = session
        try:
            progress_sink = self._build_session_sink(issue.id or "")
            run_timeout_seconds = self.workflow.agent.run_timeout_ms / 1000.0
            session.timeout_deadline_at = time.time() + run_timeout_seconds
            await asyncio.wait_for(
                self.agent_runner.run(
                    session,
                    self.workflow,
                    status_dashboard=self.status_dashboard,
                    tracker=self.tracker,
                    comment_tracker=self.tracker,
                    clarification_resolver=self._clarification_resolver,
                    progress_reporter=progress_sink,
                    diagnostics_callback=self._update_run_diagnostics,
                ),
                timeout=run_timeout_seconds,
            )
        except Exception as exc:
            logger.error(
                "Issue %s rebase-resolution: run_session raised %s",
                issue.id,
                exc,
            )
        finally:
            self._state.running.pop(issue.id or "", None)
            # Completion handling. Without this the record kept
            # has_conflict=True forever -> the next poll re-launched an
            # agent_rebase run in an infinite loop (repeated "Run in
            # progress" placeholder comments + 任务已启动/任务完成
            # oscillation on IM), and the PR link never reached IM.
            # Detect resolution via git ground-truth (not session.status),
            # clear the conflict on success, and emit a PR-link-bearing
            # event either way.
            try:
                await self._finalize_rebase_resolution(issue, session)
            except Exception:
                logger.exception(
                    "Issue %s rebase-resolution finalizer failed",
                    issue.id,
                )

    async def _finalize_rebase_resolution(
        self,
        issue: Issue,
        session: AgentSession,
    ) -> None:
        """Post-run completion handling for an ``agent_rebase`` session.

        ``_launch_rebase_resolution`` historically popped the session out of
        ``_state.running`` and did nothing else. That left ``has_conflict``
        set on the registry record, so ``_process_pending_rebase_conflicts``
        re-launched a fresh agent_rebase run on every poll -> an infinite
        loop (repeated "## Orchestratord Run Summary / Run in progress."
        placeholder comments, and 任务已启动/任务完成 oscillation on IM), and
        because the rebase path never runs ``git_sync`` or emits a
        ``pr=``-bearing event, the PR link never reached Feishu/IM.

        This checks git ground-truth (NOT ``session.status`` - the
        agent_runner completion heuristics are tuned for normal issue
        runs and can misclassify a successful rebase+push as
        "no_changes_produced") and either clears the conflict + emits a
        PR-link-bearing ``pr.updated`` event, or records an unresolved
        failure so the operator can intervene.
        """
        issue_id = issue.id or ""
        record = self._registry.get(issue_id)
        workspace_path = record.workspace_path if record else None
        resolved, new_head = await self._rebase_conflict_resolved(
            workspace_path,
            previous_head=record.commit_sha if record else None,
            base_branch=record.base_branch if record else None,
            branch_name=(record.branch_name if record else None) or issue.branch_name,
        )
        pr_url = record.pr_url if record else None
        if resolved:
            self._registry.clear_conflict(issue_id)
            if new_head and record is not None:
                record.commit_sha = new_head
                record.touch()
                self._registry._save()
            self.status_dashboard.on_session_complete(issue_id)
            self._state.completed.add(issue_id)
            self._emit_im_event(
                issue_id,
                "pr.updated",
                EventLevel.SUCCESS,
                "rebase 冲突已解决，PR 已更新",
                self._issue_payload(issue, pr=pr_url, commit=new_head),
            )
            self._log_audit_event(
                issue_id=issue_id,
                event="rebase_resolved",
                mode="agent_rebase",
                reason=f"conflicts resolved, head={new_head}",
                author="daemon",
            )
            logger.info(
                "Issue %s rebase-resolution succeeded head=%s pr=%s",
                issue_id,
                new_head,
                pr_url,
            )
            return
        # Conflict not resolved - keep has_conflict so the next poll cycle
        # can retry (bounded by max_rebase_attempts_per_issue). Surface a
        # failure event WITH the PR link so the operator can intervene.
        self.status_dashboard.on_session_failed(issue_id, "rebase_unresolved")
        self._emit_im_event(
            issue_id,
            "issue.failed",
            EventLevel.WARN,
            "rebase 冲突未解决，请人工介入",
            self._issue_payload(issue, pr=pr_url),
        )
        self._log_audit_event(
            issue_id=issue_id,
            event="rebase_unresolved",
            mode="agent_rebase",
            reason="conflicts remain after agent_rebase run",
            author="daemon",
        )
        logger.warning(
            "Issue %s rebase-resolution did not resolve conflicts; "
            "has_conflict stays set for retry",
            issue_id,
        )

    async def _rebase_conflict_resolved(
        self,
        workspace_path: str | None,
        *,
        previous_head: str | None = None,
        base_branch: str | None = None,
        branch_name: str | None = None,
    ) -> tuple[bool, str | None]:
        """Check git ground-truth for whether the agent finished the rebase.

        Returns ``(resolved, new_head_sha)``. ``resolved=True`` only when
        there are no unmerged files or active sequencer, the expected base
        is an ancestor of HEAD, and the pushed remote feature ref equals the
        local HEAD.  This distinguishes a completed rebase from
        ``git rebase --abort`` and from a local-only rebase whose push failed.

        We trust git state over ``session.status``: the agent_runner
        completion heuristics (stagnation / read_only_loop /
        no_changes_produced) are tuned for normal issue runs, not rebase
        resolution - a successful conflict resolution that pushes and
        leaves a clean tree can be misclassified as "no changes produced".
        """
        if not workspace_path or not base_branch or not branch_name:
            return False, None
        repo_root = await asyncio.to_thread(get_repo_root, workspace_path)
        if not repo_root:
            return False, None

        def _check() -> tuple[bool, str | None]:
            # Unmerged files -> conflict markers still present in the worktree.
            unmerged, _, _ = _run_git(["diff", "--name-only", "--diff-filter=U"], repo_root)
            if unmerged.strip():
                return False, None
            # REBASE_HEAD is deliberately not used here. Git can retain that
            # pseudo-ref after a completed rebase, so its presence caused
            # successfully resolved conflicts to be reported as failures.
            # The sequencer's state directories are the authoritative signal
            # that a merge- or apply-backed rebase is still active.
            for state_name in ("rebase-merge", "rebase-apply"):
                state_out, _, state_rc = _run_git(
                    ["rev-parse", "--git-path", state_name],
                    repo_root,
                )
                if state_rc != 0 or not state_out:
                    return False, None
                state_path = Path(state_out)
                if not state_path.is_absolute():
                    state_path = Path(repo_root) / state_path
                if state_path.exists():
                    return False, None
            head_out, _, head_rc = _run_git(["rev-parse", "HEAD"], repo_root)
            if head_rc != 0 or not head_out.strip():
                return False, None
            head = head_out.strip()
            if previous_head and head == previous_head:
                return False, None

            current_branch, _, branch_rc = _run_git(
                ["rev-parse", "--abbrev-ref", "HEAD"],
                repo_root,
            )
            if branch_rc != 0 or current_branch.strip() != branch_name:
                return False, None

            # Query both refs together so the ancestry decision uses the
            # current remote base rather than a potentially stale
            # ``origin/<base>`` left from the initial conflict attempt.
            remote_out, _, remote_rc = _run_git(
                [
                    "ls-remote",
                    "--heads",
                    "origin",
                    f"refs/heads/{base_branch}",
                    f"refs/heads/{branch_name}",
                ],
                repo_root,
            )
            if remote_rc != 0:
                return False, None
            remote_lines = [line.split() for line in remote_out.splitlines() if line.strip()]
            remote_heads = {parts[1]: parts[0] for parts in remote_lines if len(parts) >= 2}
            remote_base = remote_heads.get(f"refs/heads/{base_branch}")
            remote_feature = remote_heads.get(f"refs/heads/{branch_name}")
            if not remote_base or remote_feature != head:
                return False, None

            # A completed rebase must contain the current target tip.  An
            # aborted rebase returns to the old feature head and fails this
            # ancestry check even though the worktree itself is clean.
            _, _, ancestor_rc = _run_git(
                ["merge-base", "--is-ancestor", remote_base, head],
                repo_root,
            )
            if ancestor_rc != 0:
                return False, None
            return True, head

        return await asyncio.to_thread(_check)

    def _prepare_rebase_session(self, session: AgentSession) -> None:
        """Copy registry conflict metadata onto the session.

        Sets ``session.conflict_files`` from the registry so the
        agent runner / prompt builder can read which files git
        left in conflict state and inject them into the prompt.
        """
        record = self._registry.get(session.issue.id or "")
        if record is None:
            session.conflict_files = ()
            return
        session.conflict_files = tuple(record.conflict_files)

    async def _handle_rebase_control(self, issue_id: str, extra: str) -> None:
        """Handle a CLI-written rebase control file.

        Format::

            rebase
            <issue_id>
            force=0|1
            <reason>

        Routes through ``_process_rebase_intent`` so the orchestrator
        itself performs the rebase (no agent for clean rebases).
        Conflict results flow back into the registry and are picked
        up by ``_process_pending_rebase_conflicts`` on the next
        poll.
        """
        if not issue_id:
            return
        record = self._registry.get(issue_id)
        if record is None:
            logger.warning("rebase control: issue %s not in registry", issue_id)
            return
        if issue_id in self._state.running:
            logger.info(
                "rebase control: issue %s already running, skipping",
                issue_id,
            )
            return
        if not record.pr_number or not record.workspace_path or not record.branch_name:
            logger.warning(
                "rebase control: issue %s missing pr_number/workspace/branch",
                issue_id,
            )
            return

        force = False
        reason = ""
        if extra:
            for line in extra.split("\n"):
                token = line.strip()
                if token.startswith("force="):
                    force = token.split("=", 1)[1].strip() in ("1", "true", "yes")
                elif token:
                    reason = token
        logger.info(
            "rebase control: dispatching issue_id=%s force=%s reason=%r",
            issue_id,
            force,
            reason,
        )
        issue = await self.tracker.fetch_issue_states_by_ids([issue_id])
        issue_obj = issue.get(issue_id) if issue else None
        if issue_obj is None:
            issue_obj = Issue(
                id=issue_id,
                identifier=record.issue_identifier,
                title="(unknown)",
                branch_name=record.branch_name,
            )
        # The CLI already enforced the rate-limit preview; honor the
        # operator's explicit --force when set.
        await self._process_rebase_intent(issue_obj, force=force)

    def _prepare_intent_session(self, session: AgentSession) -> None:
        """Wire the session for an intent-driven run.

        Called from `_launch_issue` immediately after the AgentSession
        is constructed. Reads the registry's intent field and:

          - Intent.FOLLOWUP → set `run_kind = "agent_followup"`, copy
            the existing PR (number + url) and base_branch onto the
            session, and pin `issue.branch_name` to the registry
            branch so `_ensure_work_branch` reuses it.
          - Intent.RETRY → the registry was already reset by
            `_prepare_intent_reset`; nothing more to do here. The
            session is a fresh issue-style run.
          - Intent.NONE / Intent.BLOCKED → no-op.

        Sub-C mirrors the review_followup pattern (see
        `_launch_review_followup`): we reuse the same branch + PR
        and append a commit via git_sync(mode="followup").
        """
        issue_id = session.issue.id or ""
        if not issue_id:
            return
        record = self._registry.get(issue_id)
        if record is None or record.intent is not Intent.FOLLOWUP:
            return

        session.run_kind = (
            "review_retry" if record.last_command == "/issue review --reject" else "agent_followup"
        )

        # Wire the existing PR so git_sync reuses it instead of
        # creating a new one.
        if record.pr_number:
            session.pull_request = PullRequestRef(
                number=record.pr_number,
                url=record.pr_url,
            )

        # Pin base_branch so git_sync.push targets the right base.
        if record.base_branch:
            session.base_branch = record.base_branch

        # Pin issue.branch_name so _ensure_work_branch reuses the
        # existing feature branch (otherwise it would fall back to
        # the default name and create a new one).
        if record.branch_name and hasattr(session.issue, "branch_name"):
            try:
                session.issue.branch_name = record.branch_name
            except Exception:
                # Issue is a frozen dataclass in some contexts; in
                # that case the registry's branch_name still wins
                # because git_sync.sync also reads from the
                # registry-aware session.base_branch.
                logger.debug(
                    "Could not pin issue.branch_name for followup "
                    "issue %s; relying on session.base_branch",
                    issue_id,
                )

        # Wire feedback metadata so git_sync writes review-id /
        # review-body into the commit message.  pending_feedback_ids
        # are the unprocessed review comments that prompted this
        # follow-up; feedback_commit_body is unavailable here (the
        # registry stores IDs, not body text) so review-body: is
        # omitted for agent_followup — review-pr: is still written.
        session.feedback_ids = list(record.pending_feedback_ids)

        logger.info(
            "Issue %s followup: session wired (branch=%s pr=%s base=%s)",
            issue_id,
            getattr(session.issue, "branch_name", None),
            getattr(getattr(session, "pull_request", None), "number", None),
            session.base_branch,
        )

    async def _complete_read_only_chat_followup(
        self,
        session: AgentSession,
    ) -> None:
        """Finish a conversational follow-up that produced no new commit.

        Chat follow-ups may legitimately ask the agent to inspect, explain,
        or verify existing work.  Their useful result is the persisted reply,
        so a clean workspace must return the issue to its review gate instead
        of being misclassified as an empty implementation failure.
        """
        issue_id = session.issue.id or ""
        self._registry.update_report(
            issue_id,
            report_path=getattr(session, "report_path", None),
            verification_status=getattr(session, "verification_status", None),
            verification_output=getattr(session, "verification_output", None),
            summary_comment_id=getattr(session, "summary_comment_id", None),
            session_end_reason=getattr(session, "session_end_reason", None),
            session_end_summary=getattr(session, "session_end_summary", ""),
        )
        self._registry.increment_followup_attempt(issue_id)
        self._registry.mark_pending_review(issue_id)
        self._state.pending_review.add(issue_id)
        await self._sync_tracker_issue_state(issue_id, "pending_review")
        self.status_dashboard.on_session_complete(issue_id)
        logger.info(
            "Issue %s read-only chat follow-up completed; returning to pending review",
            issue_id,
        )

    async def _process_review_feedback(self) -> None:
        config = self.workflow.review_feedback
        if not config.enabled:
            return
        available_slots = self._state.max_concurrent_agents - len(self._state.running)
        if available_slots <= 0:
            return

        service = ReviewFeedbackService(
            tracker=self.tracker,
            registry=self._registry,
            config=config,
        )
        try:
            followups = await service.collect_followups(available_slots)
        except Exception as exc:
            logger.error("Failed to collect PR review feedback: %s", exc)
            return

        for followup in followups:
            issue_id = followup.issue.id or ""
            if issue_id in self._state.running or issue_id in self._state.claimed:
                continue
            if config.mode != "auto":
                self._registry.mark_feedback_pending(
                    issue_id,
                    [item.id for item in followup.feedback],
                    feedback_urls={item.id: item.url for item in followup.feedback if item.url},
                )
                logger.info(
                    "PR feedback pending manual follow-up issue_id=%s feedback_count=%d",
                    issue_id,
                    len(followup.feedback),
                )
                continue
            self._state.claimed.add(issue_id)
            await self._launch_review_followup(followup)

    async def _launch_followup_with_pending_reviews(self, issue: Issue) -> bool:
        """Fetch the PR's unprocessed review feedback and launch a review
        follow-up if any exists. Returns True when a follow-up was launched
        (or the issue has no PR to inspect), False when there is nothing to
        process — so the caller does NOT fall back to a full issue re-run.
        """
        record = self._registry.get(issue.id or "")
        if record is None or not record.pr_number:
            return False
        pull_request = PullRequestRef(
            number=record.pr_number,
            url=record.pr_url,
        )
        try:
            feedback = await self.tracker.fetch_pull_request_feedback(
                pull_request=pull_request,
                issue_id=record.issue_id,
                include_ci_failures=True,
            )
        except Exception as exc:  # noqa: BLE001 — best-effort collection
            logger.warning(
                "Issue %s follow-up: failed to collect PR review feedback: %s",
                issue.id or "",
                exc,
            )
            return False
        processed = set(record.processed_feedback_ids)
        pending = [
            fb
            for fb in feedback
            if fb.id not in processed
            and not self._registry.feedback_abandoned(record.issue_id, fb.id)
        ]
        # 达失败阈值的检视（已放弃重试）：回复放弃原因并标记为已处理
        # （放弃=处理完——不再反复触发——符合"每条检视都有最终回复"）。
        abandoned = [
            fb
            for fb in feedback
            if self._registry.feedback_abandoned(record.issue_id, fb.id)
        ]
        if abandoned:
            for fb in abandoned:
                try:
                    await self.tracker.reply_to_pull_request_feedback(
                        feedback=fb,
                        body="（编排器）该检视经多次处理仍未解决——已放弃自动重试。"
                        "请人工确认或重新提出。",
                    )
                except Exception:  # noqa: BLE001 — best-effort reply
                    pass
            self._registry.mark_feedback_processed(
                record.issue_id,
                [fb.id for fb in abandoned],
            )
        if not pending:
            return False
        followup = ReviewFollowup(
            issue=Issue(
                id=record.issue_id,
                identifier=record.issue_identifier,
                title=record.issue_identifier,
                branch_name=record.branch_name,
            ),
            record=record,
            pull_request=pull_request,
            feedback=pending,
        )
        await self._launch_review_followup(followup)
        return True

    async def _launch_review_followup(self, followup: ReviewFollowup) -> None:
        issue = followup.issue
        issue.branch_name = followup.record.branch_name
        prompt = PromptBuilder.render_review_feedback(
            issue=issue,
            pull_request=followup.pull_request,
            branch_name=followup.record.branch_name or "",
            feedback=followup.feedback,
        )
        try:
            workspace = await self.workspace.create_for_issue(issue)
        except Exception as exc:
            logger.error(
                "Workspace creation failed for PR follow-up issue_id=%s: %s", issue.id, exc
            )
            self._state.claimed.discard(issue.id or "")
            return
        start_commit_sha = await self.workspace.current_head(workspace.path)

        # Select per-stage runner when configured, else fall back to
        # the main agent runner (backward-compatible).
        runner = self.stage_runners.get("review_followup", self.agent_runner)
        session = AgentSession(
            issue=issue,
            workspace=workspace,
            conversation_id=followup.record.conversation_id,
            parent_run_id=followup.record.run_id,
            pause_resume_event=asyncio.Event(),
            event_queue=asyncio.Queue(),
            prompt_override=prompt,
            run_kind="review_followup",
        )
        session.pull_request = followup.pull_request
        session.base_branch = followup.record.base_branch
        session.start_commit_sha = start_commit_sha
        session.feedback_ids = [item.id for item in followup.feedback]
        # Use the first feedback body as the commit message for descriptive titles
        first_body = (followup.feedback[0].body or "").strip() if followup.feedback else ""
        session.feedback_commit_body = first_body
        self._state.running[issue.id or ""] = session
        if self._registry.mark_running(issue.id or "") is None:
            logger.warning(
                "Review follow-up started without registry record issue_id=%s",
                issue.id,
            )
        followup_record = self._registry.increment_followup_attempt(issue.id or "")
        session.issue_attempt = max(1, getattr(followup.record, "attempt_count", 0) + 1)
        session.followup_attempt = (
            followup_record.followup_attempt_count if followup_record is not None else 1
        )
        self._sync_gitignore_to_workspace(session.workspace)
        self.status_dashboard.on_session_start(
            SessionStatus(
                issue_id=issue.id or "",
                issue_identifier=issue.identifier or "",
                max_turns=self.agent_runner.max_turns,
                workspace_path=str(workspace.path),
            )
        )
        task = asyncio.create_task(self._run_issue(session))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        # Root-cause fix: register issue_id → task mapping so the
        # stop command can cancel a specific running issue.
        issue_id = issue.id or ""
        self._issue_tasks[issue_id] = task

        def _unregister_issue_task(t: asyncio.Task) -> None:
            self._issue_tasks.pop(issue_id, None)

        task.add_done_callback(_unregister_issue_task)

    async def _launch_issue(self, issue: Issue) -> None:
        """Create workspace and run agent for one issue."""
        if not await self._dependencies_satisfied(issue):
            self._state.claimed.discard(issue.id)
            return

        # If the registry carries a RETRY intent for this
        # issue, close the existing remote PR (best-effort) and reset
        # the local record so the new run starts from a clean slate.
        # This must happen BEFORE workspace creation so the new run
        # does not try to push a follow-up commit to a closed PR.
        await self._prepare_intent_reset(issue)

        workspace_strategy = self.workflow.workspace.strategy
        branch_name = getattr(issue, "branch_name", None)
        if not branch_name:
            branch_name = self.git_sync._default_branch_name(issue)
            issue.branch_name = branch_name

        try:
            workspace = await self.workspace.create_for_issue(issue)
        except Exception as exc:
            logger.error(
                "Workspace creation failed issue_id=%s: %s",
                issue.id,
                exc,
            )
            self._state.claimed.discard(issue.id)
            return

        # Register as pending so restart won't re-launch this issue
        base_branch = (
            getattr(issue, "base_branch", None) or self.workflow.workspace.base_branch or "main"
        )
        integration_branch = self.workflow.workspace.integration_branch
        if workspace_strategy == "sequential" and integration_branch:
            branch_name = integration_branch
        start_commit_sha = await self.workspace.current_head(workspace.path)
        base_commit_sha = start_commit_sha if workspace_strategy == "sequential" else None
        previous_issue_id = None
        sequence_index = None
        if workspace_strategy == "sequential":
            previous_record = self._registry.latest_sequential_record()
            previous_issue_id = previous_record.issue_id if previous_record else None
            sequence_index = (previous_record.sequence_index or 0) + 1 if previous_record else 1
        # In sequential mode the registry's workspace_path must
        # record the configured root (not whatever WorkspaceManager
        # happened to return for the current issue), so that subsequent
        # issues can resolve the previous commit chain against the same
        # path. In isolated / shared modes the per-issue workspace.path
        # is already the canonical location, so keep that.
        recorded_workspace_path = (
            str(self._workspace_root) if workspace_strategy == "sequential" else str(workspace.path)
        )
        existing_record = self._registry.get(issue.id or "")
        parent_run_id = (
            existing_record.run_id
            if existing_record is not None and existing_record.run_id
            else (existing_record.previous_run_ids[-1] if existing_record and existing_record.previous_run_ids else None)
        )
        record = self._registry.register(
            issue_id=issue.id or "",
            issue_identifier=issue.identifier or "",
            branch_name=branch_name,
            base_branch=base_branch,
            workspace_strategy=workspace_strategy,
            workspace_path=recorded_workspace_path,
            base_commit_sha=base_commit_sha,
            start_commit_sha=start_commit_sha,
            previous_issue_id=previous_issue_id,
            sequence_index=sequence_index,
            author_login=issue.author_login,
        )

        # Pre-check: verify issue is still in an active state and has no
        # existing PR (which would mean it was already handled) before running agent
        try:
            refreshed = await self.tracker.fetch_issue_states_by_ids([issue.id])
            refreshed_issue = refreshed.get(issue.id)
            if refreshed_issue is None:
                logger.info("Issue %s no longer exists, skipping", issue.id)
                self._state.claimed.discard(issue.id)
                return
            active_states = [
                s.strip().lower() for s in (getattr(self.tracker, "active_states", None) or [])
            ]
            is_active = (
                refreshed_issue.state is not None
                and refreshed_issue.state.strip().lower() in active_states
            )
            if not is_active:
                logger.info(
                    "Issue %s is no longer active (state=%r), skipping",
                    issue.id,
                    refreshed_issue.state,
                )
                self._state.claimed.discard(issue.id)
                return
            # Check for existing PR (only for repository-backed trackers)
            branch_name = refreshed_issue.branch_name
            if branch_name and supports(self.tracker, PullRequestCapability):
                base_branch = getattr(refreshed_issue, "base_branch", "main") or "main"
                existing_pr = await self.tracker.find_pull_request(
                    head_branch=branch_name,
                    base_branch=base_branch,
                )
                if existing_pr is not None:
                    # Explicit follow-up and retry intents both bypass the
                    # ordinary existing-PR guard. Follow-up reuses the PR;
                    # retry already attempted to close it and must still
                    # proceed when that best-effort close was a no-op.
                    record = self._registry.get(issue.id or "")
                    if record and record.intent in (Intent.RETRY, Intent.FOLLOWUP):
                        logger.info(
                            "Issue %s %s intent on existing PR %s (%s), proceeding",
                            issue.id,
                            record.intent.value,
                            existing_pr.number,
                            existing_pr.url,
                        )
                    else:
                        logger.info(
                            "Issue %s already has PR %s (%s), skipping",
                            issue.id,
                            existing_pr.number,
                            existing_pr.url,
                        )
                        self._state.claimed.discard(issue.id)
                        # Also add to completed so we don't re-process after restart
                        self._state.completed.add(issue.id)
                        return

            # Registry-based guard: skip if the local registry already records
            # a PR or a terminal state for this issue.  The tracker-based check
            # above only fires when the issue body contains ``branch_name:``
            # — many issues lack that field, so this tag-team guard across all
            # entry points (poll, retry queue, escalation) catches the gap.
            #
            # The ``register()`` call at line 1217 preserves ``pr_number`` from
            # any previous run (see issue_registry.py:317), while explicit retry
            # intents (``_prepare_intent_reset`` → ``reset_for_retry``) clear it
            # beforehand so a deliberate re-run still passes through.
            if self._registry.has_pr(issue.id or "") or self._registry.is_terminal(issue.id or ""):
                # Explicit retry/follow-up intents deliberately bypass the
                # handled guard. Retry clears stale PR state before reaching
                # this point; follow-up reuses it.
                record = self._registry.get(issue.id or "")
                if record and record.intent in (Intent.RETRY, Intent.FOLLOWUP):
                    logger.info(
                        "Issue %s %s intent bypasses registry guard "
                        "(has_pr=%s, is_terminal=%s), proceeding",
                        issue.id,
                        record.intent.value,
                        self._registry.has_pr(issue.id or ""),
                        self._registry.is_terminal(issue.id or ""),
                    )
                else:
                    logger.info(
                        "Issue %s already handled (registry: has_pr=%s, "
                        "is_terminal=%s), skipping via _launch_issue guard",
                        issue.id,
                        self._registry.has_pr(issue.id or ""),
                        self._registry.is_terminal(issue.id or ""),
                    )
                    self._state.claimed.discard(issue.id)
                    self._state.completed.add(issue.id)
                    return

            # Update issue with latest state
            issue.state = refreshed_issue.state
        except Exception as exc:
            logger.warning(
                "Could not verify issue state for %s: %s — proceeding anyway",
                issue.id,
                exc,
            )

        session = AgentSession(
            issue=issue,
            workspace=workspace,
            conversation_id=record.conversation_id,
            parent_run_id=parent_run_id,
            pause_resume_event=asyncio.Event(),
            event_queue=asyncio.Queue(),
        )

        # Wire pause-state notification so the socket path
        # (_drain_control_commands in agent_runner) can sync the
        # registry when pause/resume is processed.
        def _on_pause_change(issue_id: str, paused: bool, reason: str) -> None:
            if paused:
                self._registry.mark_paused(issue_id, reason=reason)
            else:
                self._registry.mark_resumed(issue_id)

        session._on_pause_state_change = _on_pause_change
        clarification_record = self._registry.get(issue.id or "")
        if clarification_record is not None and clarification_record.local_answer:
            session.clarification_answer = clarification_record.local_answer
            session.clarification_source = clarification_record.local_answer_source
            if clarification_record.question_history:
                session.clarification_question = "\n".join(
                    f"- {question}" for question in clarification_record.question_history
                )
        retry_attempt = self._state.retry_attempts.get(issue.id or "", 0)
        session.attempt = retry_attempt + 1
        session.issue_attempt = session.attempt
        session.workspace_strategy = workspace_strategy
        session.workspace_path = str(workspace.path)
        session.start_commit_sha = start_commit_sha
        session.base_commit_sha = base_commit_sha
        session.previous_issue_id = previous_issue_id
        session.sequence_index = sequence_index
        session.integration_branch = integration_branch
        session.base_branch = base_branch
        # Collaboration mode selection. Phase 1 ships only the
        # ``single`` mode; ModeSelector returns "single" unless the issue
        # carries a ``mode:<name>`` label that maps to a registered
        # runner. The decision is recorded on the session for the
        # dispatcher in ``_run_issue`` and on the registry record for
        # audit (`issue list --mode`, dashboard column).
        try:
            mode_decision = self._mode_selector.choose(issue)
        except Exception:
            logger.exception(
                "Issue %s ModeSelector.choose raised; defaulting to single",
                issue.id,
            )
            mode_decision = ModeDecision(
                mode=DEFAULT_MODE,
                reason="ModeSelector.choose raised; see logs",
                source="fallback",
            )
        session.collaboration_mode = mode_decision.mode
        session.mode_decision = mode_decision
        record = self._registry.get(issue.id or "")
        if record is not None:
            record.collaboration_mode = mode_decision.mode
            record.mode_decision_reason = mode_decision.reason
            record.touch()
            self._registry._save()
        logger.info(
            "Issue %s collaboration_mode=%s (source=%s, reason=%s)",
            issue.id,
            mode_decision.mode,
            mode_decision.source,
            mode_decision.reason,
        )
        if self._viz_journal is not None:
            self._viz_journal.write_event(
                {
                    "type": "issue_status",
                    "issue_id": str(issue.id or ""),
                    "status": "running",
                }
            )
            self._viz_journal.write_event(
                {
                    "type": "phase",
                    "issue_id": str(issue.id or ""),
                    "phase": f"mode:{mode_decision.mode}",
                }
            )
        # 统一：intent FOLLOWUP（命令/重试）一律走检视意见处理（review_followup
        # ——fetch pending 检视——带检视）——不再启动 agent_followup 重跑任务
        # （重跑 issue 仅由 /agent retry 承担）。
        followup_record = self._registry.get(issue.id or "")
        if (
            followup_record is not None
            and followup_record.intent == Intent.FOLLOWUP
        ):
            followup_handled = await self._launch_followup_with_pending_reviews(issue)
            if not followup_handled:
                logger.info(
                    "Issue %s follow-up: no pending review feedback to process — skip",
                    issue.id or "",
                )
            return
        # If the registry intent is FOLLOWUP, wire the
        # session so the agent + git_sync know to reuse the existing
        # branch / PR rather than create a new run.
        self._prepare_intent_session(session)
        if session.run_kind == "review_followup":
            session.stage_id = "review_followup"
        # Retry context: propagate previous_run_ids from the registry
        # to the session so the prompt builder can inject them.
        prev_record = self._registry.get(issue.id or "")
        if prev_record and prev_record.previous_run_ids:
            session.previous_run_ids = list(prev_record.previous_run_ids)
        # Also propagate the last run's verification failure output (e.g.
        # pre-commit gate failure) so the retry prompt can carry it directly
        # to the agent — not just as a readable transcript hint.
        if prev_record is not None:
            session.previous_verification_error = (
                getattr(prev_record, "verification_output", None)
                or getattr(prev_record, "last_hook_error", None)
            )
        self._state.running[issue.id] = session

        # Update persistent registry so `issue list` reflects running state
        self._registry.mark_running(issue.id or "")

        # Sync .gitignore to workspace so unwanted files are excluded from commit
        self._sync_gitignore_to_workspace(session.workspace)

        self.status_dashboard.on_session_start(
            SessionStatus(
                issue_id=issue.id or "",
                issue_identifier=issue.identifier or "",
                max_turns=self.agent_runner.max_turns,
                workspace_path=str(workspace.path),
            )
        )

        task = asyncio.create_task(self._run_issue(session))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        # Register issue_id → task mapping so the stop command
        # can cancel a specific running issue via task.cancel().
        issue_id_task = issue.id or ""
        self._issue_tasks[issue_id_task] = task

        def _unregister_issue_task(t: asyncio.Task) -> None:
            self._issue_tasks.pop(issue_id_task, None)

        task.add_done_callback(_unregister_issue_task)

    async def _sync_tracker_issue_state(self, issue_id: str, state: str) -> bool:
        if not issue_id:
            return False
        try:
            await self.tracker.update_issue_state(issue_id, state)
            return True
        except Exception as exc:
            logger.warning(
                "Failed to sync tracker state issue_id=%s state=%s: %s",
                issue_id,
                state,
                exc,
            )
            return False

    def _update_run_diagnostics(self, session: AgentSession) -> None:
        issue_id = session.issue.id or ""
        record = self._registry.update_run_diagnostics(
            issue_id,
            run_id=getattr(session, "run_id", None),
            debug_log_path=getattr(session, "debug_log_path", None),
            turn_count=getattr(session, "turn_count", 0),
            tool_count=getattr(session, "tool_count", 0),
            last_event=getattr(session, "last_agent_event", None),
            last_tool=getattr(session, "last_tool_name", None),
            output_len=len(getattr(session, "output_text", "") or ""),
            timeout_deadline_at=getattr(session, "timeout_deadline_at", None),
            workspace_dirty=getattr(session, "run_workspace_dirty", None),
            cost_usd=getattr(session, "cost_usd", None),
            token_usage=getattr(session, "token_usage", None),
            started_at=getattr(session, "started_at", None),
            completed_at=getattr(session, "completed_at", None),
            duration_ms=getattr(session, "duration_ms", None),
            backend=getattr(session, "_snapshot_provider", None) or None,
            model=getattr(session, "_snapshot_model", None) or None,
        )
        if record is None:
            if not (issue_id or "").startswith("stage-"):
                logger.warning(
                    "Skipped run diagnostics update because registry record is missing issue_id=%s run_id=%s status=%s",
                    issue_id,
                    getattr(session, "run_id", None),
                    getattr(session, "status", None),
                )

    async def _run_issue_with_workflow(
        self,
        session: AgentSession,
        progress_sink: Any,
    ) -> None:
        """使用声明式工作流引擎处理 issue。

        通过 WorkflowOrchestrator 按 workflow.yaml 定义的 DAG 阶段
        执行 issue，每个阶段由 AgentRunner 驱动的合成 Issue 执行。
        """
        workflow_orch = self._workflow_orchestrator
        if workflow_orch is None:
            logger.error("_run_issue_with_workflow called but no workflow orchestrator")
            session.status = "failed"
            return

        logger.info(
            "Running workflow for issue %s: %s",
            session.issue.identifier,
            session.issue.title,
        )

        # 确保 workspace 在 issue 分支上（非主分支）。
        # 保留的工作区可能还在 main 或上一次运行的分支上，
        # 必须在 workflow 执行前切换到正确的 issue 分支。
        try:
            work_branch = self.git_sync._ensure_work_branch(
                str(session.workspace.path),
                session.issue,
                session.base_branch or get_default_branch(str(session.workspace.path)),
            )
            logger.info(
                "Workflow workspace on branch: %s (issue=%s)",
                work_branch,
                session.issue.identifier,
            )
        except Exception as exc:
            logger.warning(
                "Failed to ensure work branch for workflow issue %s: %s",
                session.issue.id,
                exc,
            )

        # 将编排器的 ProgressSink 注入工作流引擎，
        # 使阶段进度实时反映到 StatusDashboard
        workflow_orch.set_progress_sink(progress_sink)
        workflow_orch._stage_runner._progress_reporter = progress_sink

        try:
            result = await workflow_orch.run_for_issue(
                issue=session.issue,
                workspace_path=str(session.workspace.path),
            )
        except Exception as exc:
            logger.exception("Workflow execution failed for issue %s", session.issue.id)
            session.status = "failed"
            session.output_text = str(exc)
            return

        # 将阶段输出存储到 session，供 git_sync 写入 PR body
        session.workflow_stage_outputs = {}
        for stage_id, stage_result in result.stage_results.items():
            if stage_result.outputs:
                session.workflow_stage_outputs[stage_id] = {
                    "phase": getattr(workflow_orch.schema.get_stage(stage_id), "phase", ""),
                    "name": getattr(
                        workflow_orch.schema.get_stage(stage_id), "name", f"Stage {stage_id}"
                    ),
                    "output": stage_result.outputs[0] if stage_result.outputs else "",
                }

        if result.success:
            session.status = "completed"
            session.output_text = (
                f"Workflow '{result.workflow_name}' completed: "
                f"{result.completed_stages}/{result.total_stages} stages, "
                f"cost=${result.total_cost_usd:.4f}, "
                f"duration={result.total_duration_seconds:.1f}s"
            )
        else:
            session.status = "failed"
            session.output_text = (
                f"Workflow '{result.workflow_name}' failed at stage "
                f"{result.completed_stages}/{result.total_stages}: {result.error}"
            )

        self._update_run_diagnostics(session)

    def _repro_gate_applies(self, session: AgentSession) -> bool:
        """The gate only fronts fresh issue runs (not retries of other
        run kinds), only when enabled, and — when ``labels`` is
        configured — only for issues carrying one of those labels."""
        config = self.workflow.agent.repro_first
        if not config.enabled or session.run_kind != "issue":
            return False
        if config.labels:
            issue_labels = {
                label.strip().lower() for label in (getattr(session.issue, "labels", None) or [])
            }
            wanted = {label.strip().lower() for label in config.labels}
            if not issue_labels & wanted:
                return False
        return True

    async def _run_repro_gate(self, session: AgentSession, progress_sink: Any) -> bool:
        """Run the reproduction stage; True means "bug demonstrated,
        proceed to the fix stage".

        On a closed gate the issue is marked FAILED with a
        "cannot reproduce" report posted to the tracker, mirroring the
        empty-branch failure path (no MR is opened).
        """
        issue = session.issue
        config = self.workflow.agent.repro_first
        session.run_kind = "repro"
        session.prompt_override = build_repro_prompt(issue)
        repro_timeout_seconds = config.timeout_ms / 1000.0
        session.timeout_deadline_at = time.time() + repro_timeout_seconds
        logger.info("Issue %s: repro-first gate starting", issue.id)
        timed_out = False
        try:
            await asyncio.wait_for(
                self.agent_runner.run(
                    session,
                    self.workflow,
                    status_dashboard=self.status_dashboard,
                    # The repro stage has its own executable completion
                    # contract below. Passing the tracker here makes the
                    # generic runner continue while the issue is still open,
                    # even after the repro artifacts are complete.
                    tracker=None,
                    comment_tracker=self.tracker,
                    clarification_resolver=self._clarification_resolver,
                    progress_reporter=progress_sink,
                    diagnostics_callback=self._update_run_diagnostics,
                ),
                timeout=repro_timeout_seconds,
            )
        except asyncio.TimeoutError:
            timed_out = True
            logger.warning("Issue %s: repro stage timed out", issue.id)

        result = ReproGateResult(verdict="missing")
        if not timed_out:
            result = await evaluate_repro_gate(
                session.workspace.path,
                timeout_ms=config.command_timeout_ms,
            )

        if result.proceed:
            assert result.command is not None
            logger.info(
                "Issue %s: reproduction established (%s) — opening fix stage",
                issue.id,
                result.command,
            )
            session.repro_command = result.command
            append_repro_hint(session.workspace.path, result.command)
            # Reset per-run state so the fix stage gets a clean session
            # (mirrors the pipeline mode's between-stage reset).
            session.turn_count = 0
            session.status = "running"
            session.output_text = ""
            session.session_end_reason = None
            session.session_end_summary = ""
            session.run_id = None
            session.consecutive_429_count = 0
            session.rate_limit_pending_turn = None
            session.prompt_override = None
            session.run_kind = "issue"
            return True

        verdict = "repro_stage_timeout" if timed_out else result.verdict
        logger.warning(
            "Issue %s: repro-first gate closed (verdict=%s) — marking FAILED "
            "without attempting a fix",
            issue.id,
            verdict,
        )
        session.status = "failed"
        session.session_end_reason = "not_reproducible"
        session.session_end_summary = f"repro gate closed: {verdict}"
        self._registry.mark_failed_with_reason(
            issue.id or "",
            f"not_reproducible ({verdict}): the described behavior could not "
            "be demonstrated; no fix attempted, no PR created.",
        )
        try:
            await self.tracker.create_comment(
                issue.id or "",
                format_repro_gate_comment(issue, result),
            )
        except Exception:
            logger.warning(
                "Issue %s: failed to post repro-gate comment",
                issue.id,
                exc_info=True,
            )
        await self._sync_tracker_issue_state(issue.id or "", "failed")
        self.status_dashboard.on_session_complete(issue.id or "")
        self._state.completed.add(issue.id or "")
        self._state.failed.add(issue.id or "")
        return False

    def _resolve_session_runner(self, session: AgentSession) -> Any:
        """Resolve the requested runner without silently changing semantics."""
        collab_mode = getattr(session, "collaboration_mode", None) or DEFAULT_MODE
        if collab_mode != "single" and session.run_kind == "issue":
            try:
                return _modes.get(collab_mode)
            except KeyError as exc:
                raise RuntimeError(
                    f"Issue {session.issue.id} requested collaboration mode "
                    f"{collab_mode!r}, but that mode is not enabled in workflow.md"
                ) from exc
        return self.stage_runners.get(session.run_kind, self.agent_runner)

    def _resolve_runner(self, task: AgentTask) -> AgentTaskRunner:
        """Resolve the capability runner for a generic task.

        ``stage_runners`` remains a compatibility registry for legacy
        issue-runner overrides.  A registered entry is used only when it
        implements the generic protocol; otherwise generic callers always
        get the configured backend runner.
        """
        candidate = self.stage_runners.get(task.kind, self.agent_runner)
        if callable(getattr(candidate, "run_task", None)):
            return candidate
        return self.agent_runner

    async def _run_agent_task(
        self,
        task: AgentTask,
        *,
        progress_callback: Any | None = None,
        diagnostics_callback: Any | None = None,
    ) -> AgentTaskResult:
        """Run a generic task through the capability layer only.

        No tracker, Git, PR, or dashboard dependency crosses this boundary.
        Business pipelines are responsible for consuming the returned result.
        """
        runner = self._resolve_runner(task)
        return await runner.run_task(
            task,
            progress_callback=progress_callback,
            diagnostics_callback=diagnostics_callback,
        )

    async def run_task(
        self,
        task: AgentTask,
        *,
        progress_callback: Any | None = None,
        diagnostics_callback: Any | None = None,
    ) -> AgentTaskResult:
        """Execute a backend-neutral work unit through the configured runner.

        This is the public Layer-2 entry point for callers that do not
        originate from a tracker issue.  Issue polling continues to own its
        tracker and Git lifecycle, while direct task callers receive the
        structured Layer-1 result without inheriting Issue-specific hooks.
        """
        return await self._run_agent_task(
            task,
            progress_callback=progress_callback,
            diagnostics_callback=diagnostics_callback,
        )

    async def _run_issue(self, session: AgentSession) -> None:
        """Run agent for one issue with concurrency control."""
        async with self._semaphore:
            ran_agent = False
            workspace_dirty: bool | None = None
            try:
                await self.workspace.run_before_run_hook(
                    session.workspace,
                    session.issue,
                )
                # The Issue-to-PR pipeline owns the mapping from tracker
                # data to generic work.  Legacy runners still receive the
                # session for lifecycle compatibility, but prompt building
                # and all new capability code consume ``session.task``.
                session.task = issue_to_agent_task(
                    session.issue,
                    attempt=session.attempt,
                    previous_run_ids=session.previous_run_ids,
                    workspace_path=str(session.workspace.path),
                    max_turns=self.workflow.agent.max_turns,
                    timeout_seconds=self.workflow.agent.run_timeout_ms / 1000.0,
                    clarification_question=session.clarification_question,
                    clarification_answer=session.clarification_answer,
                    clarification_source=session.clarification_source,
                    conflict_files=session.conflict_files,
                    prompt_override=session.prompt_override,
                    conversation_id=session.conversation_id,
                )
                ran_agent = True
                try:
                    # Build a fresh per-session progress sink so
                    # concurrent issues no longer share the
                    # ``_current_task_id`` / ``_phase_count`` mutable
                    # state of the legacy :class:`ProgressReporter`
                    # singleton. ``AgentRunner.run`` is duck-typed on
                    # the kwarg: anything with ``on_phase_complete`` /
                    # ``on_turn_complete`` / ``on_session_complete``
                    # methods works.
                    progress_sink = self._build_session_sink(session.issue.id or "")

                    # Repro-first gate: before any fix work, a dedicated
                    # reproduction pass must demonstrate the described
                    # failure (executable check, non-zero exit). A closed
                    # gate fails the issue with a "cannot reproduce"
                    # report instead of an unverifiable fix MR.
                    if self._repro_gate_applies(session):
                        gate_open = await self._run_repro_gate(session, progress_sink)
                        if not gate_open:
                            return

                    # 如果配置了 workflow.yaml，使用声明式工作流引擎
                    # review_followup 使用专用 prompt（render_review_feedback），
                    # 不走 workflow.yaml 的完整 stage 流程，避免循环。
                    if (
                        self._workflow_orchestrator is not None
                        and session.run_kind != "review_followup"
                    ):
                        await self._run_issue_with_workflow(session, progress_sink)
                    else:
                        # Collaboration-mode dispatch. For the
                        # default ``single`` mode (the only one
                        # registered in Phase 1) we keep the legacy
                        # ``stage_runners[run_kind] or agent_runner``
                        # lookup so 270+ existing tests pass byte-
                        # identically. For non-single modes registered
                        # in later phases, we dispatch to the
                        # ``ModeRunner`` from the registry instead, and
                        runner = self._resolve_session_runner(session)
                        run_timeout_seconds = self.workflow.agent.run_timeout_ms / 1000.0
                        session.timeout_deadline_at = time.time() + run_timeout_seconds
                        await asyncio.wait_for(
                            runner.run(
                                session,
                                self.workflow,
                                status_dashboard=self.status_dashboard,
                                tracker=self.tracker,
                                comment_tracker=self.tracker,
                                clarification_resolver=self._clarification_resolver,
                                progress_reporter=progress_sink,
                                diagnostics_callback=self._update_run_diagnostics,
                            ),
                            timeout=run_timeout_seconds,
                        )
                    if session.status in (
                        "completed",
                        "stagnation",
                        "read_only_loop",
                        "loop_detected",
                        "max_turns_exceeded",
                    ):
                        # Honest-exit channel (defect R3): the agent declared
                        # the issue premise unfulfillable (e.g. it references
                        # a file that does not exist). Report the finding back
                        # to the issue and mark FAILED instead of falling
                        # through to git_sync — which would either open an MR
                        # around a fabricated fix or an empty branch.
                        _cannot = read_cannot_proceed(getattr(session.workspace, "path", None))
                        if _cannot is not None:
                            _reason = str(_cannot.get("reason", "cannot_proceed"))
                            session.status = "failed"
                            session.session_end_reason = "premise_not_met"
                            session.session_end_summary = str(_cannot.get("details", ""))[:500]
                            logger.warning(
                                "Issue %s: agent declared cannot_proceed (%s) — "
                                "marking FAILED without creating a PR",
                                session.issue.id,
                                _reason,
                            )
                            self._registry.mark_failed_with_reason(
                                session.issue.id or "",
                                f"premise_not_met ({_reason}): agent declared the issue "
                                "cannot honestly be completed; no PR created.",
                            )
                            try:
                                await self.tracker.create_comment(
                                    session.issue.id or "",
                                    format_cannot_proceed_comment(session.issue, _cannot),
                                )
                            except Exception:
                                logger.warning(
                                    "Issue %s: failed to post cannot_proceed comment",
                                    session.issue.id,
                                    exc_info=True,
                                )
                            await self._sync_tracker_issue_state(session.issue.id or "", "failed")
                            self.status_dashboard.on_session_complete(session.issue.id or "")
                            self._state.completed.add(session.issue.id or "")
                            self._state.failed.add(session.issue.id or "")
                            return
                        # Safety net: verify workspace has actual changes before git_sync.
                        # If agent reported "completed" but workspace is clean (no uncommitted
                        # changes, no HEAD change), mark as failed to avoid empty PRs.
                        if session.status == "completed" and session.session_end_reason not in (
                            "noop_completed",
                            "already_completed",
                            "task_complete",
                        ):
                            _has_changes = False
                            try:
                                _repo_root = get_repo_root(str(session.workspace.path))
                                if _repo_root:
                                    _file_status = get_file_status(_repo_root)
                                    _has_changes = bool(_file_status)
                                    if not _has_changes:
                                        _start_sha = getattr(session, "start_commit_sha", None)
                                        if _start_sha:
                                            _head_out, _, _rc = _run_git(
                                                ["rev-parse", "HEAD"], _repo_root
                                            )
                                            _has_changes = bool(
                                                _rc == 0
                                                and _head_out.strip()
                                                and _head_out.strip() != _start_sha
                                            )
                                else:
                                    # Non-git workspace: git can't answer the
                                    # question, so fail open rather than
                                    # discarding a run that did produce files.
                                    _has_changes = True
                            except Exception:
                                _has_changes = True  # fail-open
                            if not _has_changes:
                                if session.run_kind == "agent_followup":
                                    await self._complete_read_only_chat_followup(session)
                                    return
                                logger.warning(
                                    "Session completed but workspace has no changes "
                                    "issue_id=%s — marking as failed",
                                    session.issue.id,
                                )
                                session.status = "failed"
                                session.session_end_reason = "no_changes_produced"
                                session.session_end_summary = (
                                    "Agent reported completed but workspace has no file changes"
                                )
                        # A followup run passes mode="followup"
                        # to git_sync so it reuses the existing branch + PR
                        # instead of creating a new one.
                        sync_mode = (
                            "followup"
                            if session.run_kind
                            in ("agent_followup", "review_followup", "review_retry")
                            and not isinstance(
                                self.tracker,
                                __import__(
                                    "orchestratord.local_tracker.adapter",
                                    fromlist=["LocalTrackerAdapter"],
                                ).LocalTrackerAdapter,
                            )
                            else "default"
                        )
                        sync_result = await self.git_sync.sync(session, mode=sync_mode)
                        # 补遗：daemon 触发了 read-only loop /
                        # stagnation 等终止场景时，git_sync 不会创建 PR，
                        # 并在 session_end_reason 中标记 empty_branch_no_commits。
                        # 这时不能走 mark_synced（会标 SYNCED + 无 PR），
                        # 必须走 mark_failed_with_reason，让 issue 进入 FAILED。
                        if (
                            sync_result is not None
                            and sync_result.session_end_reason == "empty_branch_no_commits"
                        ):
                            logger.warning(
                                "Issue %s ended with no reviewable commit "
                                "(session_end_reason=%s) — marking FAILED "
                                "without creating a PR",
                                session.issue.id,
                                sync_result.session_end_reason,
                            )
                            session.status = "failed"
                            session.session_end_reason = "empty_branch_no_commits"
                            session.session_end_summary = (
                                "Agent did not produce any file modifications; no PR was created."
                            )
                            session.verification_status = "failed"
                            session.verification_output = session.session_end_summary
                            session.last_hook_error = session.session_end_summary
                            return
                        if sync_result is not None:
                            self._registry.update_report(
                                session.issue.id or "",
                                report_path=getattr(session, "report_path", None),
                                verification_status=getattr(session, "verification_status", None),
                                verification_output=getattr(session, "verification_output", None),
                                summary_comment_id=getattr(session, "summary_comment_id", None),
                                # Root-cause fix: persist
                                # explicit session-end reason so the
                                # dashboard / verification can
                                # distinguish stagnation / loop from
                                # a clean success path.
                                session_end_reason=getattr(session, "session_end_reason", None),
                                session_end_summary=getattr(session, "session_end_summary", ""),
                            )
                            if session.run_kind == "review_followup":
                                self._registry.mark_feedback_processed(
                                    session.issue.id or "",
                                    list(getattr(session, "feedback_ids", [])),
                                    commit_sha=sync_result.commit_sha,
                                )
                                await self._reply_to_processed_feedback(session)
                                await self._post_feedback_summary(session, sync_result)
                                await self._apply_review_rules(session)
                            elif session.run_kind in ("agent_followup", "review_retry"):
                                # A follow-up keeps the
                                # existing pr_number / pr_url / status;
                                # only the followup_attempt_count and
                                # last_followup_commit_sha change.
                                self._registry.increment_followup_attempt(session.issue.id or "")
                                if sync_result.commit_sha:
                                    record = self._registry.get(session.issue.id or "")
                                    if record is not None:
                                        record.last_followup_commit_sha = sync_result.commit_sha
                                        self._registry._save()
                                    if session.run_kind == "review_retry":
                                        # Keep rejected-review feedback
                                        # available across failed attempts,
                                        # but consume it once a follow-up
                                        # commit has synced so a future reset
                                        # cannot replay stale advice.
                                        self._clarification_queue.consume_feedback(
                                            session.issue.id or ""
                                        )
                                logger.info(
                                    "Issue %s followup committed: %s on %s",
                                    session.issue.id,
                                    sync_result.commit_sha,
                                    sync_result.branch_name,
                                )
                            else:
                                self._registry.mark_synced(
                                    session.issue.id or "",
                                    branch_name=sync_result.branch_name,
                                    commit_sha=sync_result.commit_sha,
                                    pr_number=sync_result.pull_request.number
                                    if sync_result.pull_request
                                    else None,
                                    pr_url=sync_result.pull_request.url
                                    if sync_result.pull_request
                                    else None,
                                )
                            pr_url = (
                                sync_result.pull_request.url
                                if sync_result.pull_request is not None
                                else None
                            )
                            if pr_url:
                                is_followup = session.run_kind in (
                                    "agent_followup",
                                    "review_followup",
                                    "review_retry",
                                )
                                self._emit_im_event(
                                    session.issue.id or "",
                                    "pr.updated" if is_followup else "pr.opened",
                                    EventLevel.INFO,
                                    "PR updated" if is_followup else "PR opened",
                                    self._session_payload(
                                        session,
                                        pr=pr_url,
                                        commit=getattr(sync_result, "commit_sha", None),
                                    ),
                                )
                            # Review gate: after commit, await human review before completion.
                            # Triggered when GitSyncResult.pending_review is True (LocalTracker
                            # by default, or any tracker when agent.review_required=True in workflow).
                            if sync_result.pending_review:
                                if self.workflow.agent.auto_approve:
                                    logger.info(
                                        "Issue %s auto-approved (auto_approve=True) — "
                                        "skipping pending_review gate",
                                        session.issue.id,
                                    )
                                else:
                                    self._registry.mark_pending_review(session.issue.id or "")
                                    await self._sync_tracker_issue_state(
                                        session.issue.id or "", "pending_review"
                                    )
                                    self.status_dashboard.on_session_complete(
                                        session.issue.id or ""
                                    )
                                    self._emit_im_event(
                                        session.issue.id or "",
                                        "pr.pending_review_gate",
                                        EventLevel.WARN,
                                        "pending human review",
                                        self._session_payload(session, pr=pr_url),
                                    )
                                    self._state.pending_review.add(session.issue.id or "")
                                    # Do NOT cleanup workspace — human needs to review it
                                    return

                        # Downstream compatibility deviation (TODO upstream-merge):
                        # salvage override — when the widened gate above let
                        # us attempt git_sync for a non-completed agent
                        # termination, but the sync actually produced a real
                        # commit + PR, treat the run as a successful salvage:
                        # override session.status to "completed" and record
                        # the actual termination reason in
                        # session_end_reason / session_end_summary so the
                        # audit trail is preserved. Without this, the
                        # post-`_run_issue` failure handler would still see
                        # status=stagnation/loop_detected/etc and route the
                        # run to retry/abandoned even though the work landed.
                        # Budget-exhausted terminations (max_turns reached /
                        # exit_code=... / token exhaustion) must NOT be
                        # silently salvaged into "completed": the agent ran
                        # out of budget mid-work, so the收尾 steps (report
                        # files, pre-commit check) never ran and the PR is
                        # incomplete. Keep the run failed so retry/human
                        # review handles it and the Run Summary reflects the
                        # real termination reason instead of a fake success.
                        _end_reason = session.session_end_reason or ""
                        _budget_exhausted = (
                            _end_reason == "max_turns"
                            or _end_reason.startswith("exit_code=")
                        )
                        if (
                            session.status != "completed"
                            and not _budget_exhausted
                            and sync_result is not None
                            and sync_result.commit_sha
                        ):
                            logger.warning(
                                "Issue %s session terminated with status=%s "
                                "but git_sync salvaged commit %s on branch "
                                "%s — overriding status to completed and "
                                "recording salvage reason",
                                session.issue.id,
                                session.status,
                                sync_result.commit_sha,
                                sync_result.branch_name,
                            )
                            session.session_end_reason = f"salvaged_after_{session.status}"
                            session.session_end_summary = (
                                f"agent terminated with status="
                                f"{session.status}; git_sync salvaged "
                                f"commit {sync_result.commit_sha[:12]} on "
                                f"branch {sync_result.branch_name}"
                            )
                            session.status = "completed"
                finally:
                    await self.workspace.run_after_run_hook(
                        session.workspace,
                        session.issue,
                    )
            except GitSyncPostCommitError as exc:
                sync_result = exc.result
                self._registry.update_report(
                    session.issue.id or "",
                    report_path=getattr(session, "report_path", None),
                    verification_status=getattr(session, "verification_status", None),
                    verification_output=getattr(session, "verification_output", None),
                    summary_comment_id=getattr(session, "summary_comment_id", None),
                    session_end_reason=getattr(session, "session_end_reason", None),
                    session_end_summary=getattr(session, "session_end_summary", ""),
                )
                if session.run_kind in ("agent_followup", "review_retry"):
                    record = self._registry.get(session.issue.id or "")
                    if record is not None and sync_result.commit_sha:
                        record.last_followup_commit_sha = sync_result.commit_sha
                        self._registry._save()
                elif session.run_kind != "review_followup":
                    self._registry.mark_synced(
                        session.issue.id or "",
                        branch_name=sync_result.branch_name,
                        commit_sha=sync_result.commit_sha,
                        pr_number=(
                            sync_result.pull_request.number if sync_result.pull_request else None
                        ),
                        pr_url=(sync_result.pull_request.url if sync_result.pull_request else None),
                    )
                logger.warning(
                    "Post-commit sync failed issue_id=%s commit=%s: %s",
                    session.issue.id,
                    sync_result.commit_sha,
                    exc,
                )
                session.status = "verification_failed"
                session.verification_status = "failed"
                session.verification_output = exc.output
                if exc.hook_name:
                    session.last_hook_error = str(exc.cause)
                self._emit_im_event(
                    session.issue.id or "",
                    "post_commit_failed",
                    EventLevel.ERROR,
                    str(exc),
                    self._session_payload(
                        session,
                        pr=sync_result.pull_request.url
                        if sync_result.pull_request is not None
                        else None,
                        commit=getattr(sync_result, "commit_sha", None),
                    ),
                )
            except VerificationFailed as exc:
                logger.warning(
                    "Verification failed issue_id=%s: %s",
                    session.issue.id,
                    exc,
                )
                session.status = "verification_failed"
                session.verification_status = "failed"
                session.verification_output = exc.output
                if session.run_kind == "review_followup":
                    # 检视处理失败：逐检视失败计数 +1（达到阈值后放弃——
                    # 不再反复触发防烧 token；最终回复说明放弃原因）。
                    self._registry.increment_feedback_failure(
                        session.issue.id or "",
                        list(getattr(session, "feedback_ids", [])),
                    )
                self._emit_im_event(
                    session.issue.id or "",
                    "verification.failed",
                    EventLevel.WARN,
                    exc.output or str(exc),
                    self._session_payload(session),
                )
            except HookFailedError as exc:
                logger.warning(
                    "Hook failed issue_id=%s hook=%s: %s",
                    session.issue.id,
                    exc.hook_name,
                    exc,
                )
                session.status = "verification_failed"
                session.verification_status = "failed"
                session.verification_output = exc.output
                session.last_hook_error = str(exc)
                self._emit_im_event(
                    session.issue.id or "",
                    "verification.failed",
                    EventLevel.WARN,
                    f"{exc.hook_name}: {exc.output or exc}",
                    self._session_payload(session),
                )
            except asyncio.TimeoutError:
                reason = (
                    "Agent run exceeded configured timeout "
                    f"({self.workflow.agent.run_timeout_ms}ms)"
                )
                logger.warning(
                    "Agent run timed out issue_id=%s timeout_ms=%s",
                    session.issue.id,
                    self.workflow.agent.run_timeout_ms,
                )
                workspace_dirty = bool(get_file_status(str(session.workspace.path)))
                append_debug_event(
                    getattr(session, "debug_log_path", None),
                    "orchestrator.timeout",
                    run_id=getattr(session, "run_id", None),
                    turn_count=getattr(session, "turn_count", 0),
                    tool_count=getattr(session, "tool_count", 0),
                    last_event_type=getattr(session, "last_agent_event", None),
                    last_tool=getattr(session, "last_tool_name", None),
                    output_len=len(getattr(session, "output_text", "") or ""),
                    workspace_dirty=workspace_dirty,
                    timeout_ms=self.workflow.agent.run_timeout_ms,
                )
                session.status = "agent_timeout"
                session.verification_status = "failed"
                session.verification_output = reason
                self._emit_im_event(
                    session.issue.id or "",
                    "issue.failed",
                    EventLevel.WARN,
                    reason,
                    self._session_payload(
                        session,
                        turns=getattr(session, "turn_count", None),
                    ),
                )
            except asyncio.CancelledError:
                # Root-cause fix: clean cancellation path.
                # When the stop command cancels the task, capture
                # the reason so the registry marks the issue as
                # cancelled instead of silently dropping it.
                # Also clean up the workspace immediately to avoid
                # leaking worktrees on unexpected cancellation.
                logger.warning(
                    "Agent run cancelled issue_id=%s — cleaning up workspace",
                    session.issue.id,
                )
                session.status = "cancelled"
                session.session_end_reason = "operator_stopped"
                session.session_end_summary = "cancelled by operator"
                session.verification_status = "cancelled"
                session.verification_output = "Operator requested stop"
                # Best-effort workspace cleanup on cancellation so
                # worktrees are not left dirty even if the outer
                # finally block is skipped or interrupted.
                try:
                    issue_record = self._registry.get(session.issue.id)
                    await self.workspace.cleanup(
                        session.issue,
                        end_status=session.status,
                        end_reason=session.session_end_reason,
                        agent_config=getattr(self, "_agent_config", None),
                        issue_record=issue_record,
                    )
                except Exception as cleanup_exc:
                    logger.warning(
                        "Workspace cleanup on cancellation failed issue_id=%s: %s",
                        session.issue.id,
                        cleanup_exc,
                    )
            except Exception as exc:
                logger.exception(
                    "Agent run failed issue_id=%s: %s",
                    session.issue.id,
                    exc,
                )
                session.status = "before_run_failed" if not ran_agent else "failed"
                # Replace any prior success summary with the actual failure
                # detail so IM and registry records show the root cause.
                detail = _operator_failure_detail(exc)
                session.session_end_reason = session.status
                session.session_end_summary = detail
                session.verification_status = "failed"
                session.verification_output = detail
                session.last_hook_error = detail
                setattr(session, "operator_failure_detail", detail)
            finally:
                if workspace_dirty is not None:
                    session.run_workspace_dirty = workspace_dirty
                self._update_run_diagnostics(session)
                # Diagnostics saves are throttled; force the final
                # snapshot to disk in case this path (e.g. pending_review)
                # ends without a durable status mutation.
                self._registry.flush()

                if session.issue.id in self._state.running:
                    del self._state.running[session.issue.id]

                # Push today's telemetry summary after the run ends
                # (best-effort; no-op unless workflow.telemetry is enabled).
                self._report_telemetry()
                # Dashboard journal: one terminal event per run with the
                # final status plus the session/PR references the issue
                # accumulated. Best-effort — never raises.
                if self._viz_journal is not None:
                    try:
                        _iid = str(session.issue.id or "")
                        _rec = self._registry.get(_iid)
                        if getattr(session, "run_id", None):
                            self._viz_journal.write_event(
                                {
                                    "type": "session_ref",
                                    "issue_id": _iid,
                                    "session_id": str(session.run_id),
                                    "session_path": str(
                                        Path.home()
                                        / ".orchestratord"
                                        / "sessions"
                                        / str(session.run_id)
                                    ),
                                }
                            )
                        if _rec is not None and _rec.pr_url:
                            self._viz_journal.write_event(
                                {
                                    "type": "pr_status",
                                    "issue_id": _iid,
                                    "pr_url": _rec.pr_url,
                                    "pr_number": _rec.pr_number,
                                }
                            )
                        _status = str(session.status or "")
                        if _status == "completed":
                            self._viz_journal.write_event(
                                {
                                    "type": "complete",
                                    "issue_id": _iid,
                                    "overall_status": "completed",
                                }
                            )
                        elif _status:
                            self._viz_journal.write_event(
                                {
                                    "type": "error",
                                    "issue_id": _iid,
                                    "error": getattr(session, "session_end_summary", "") or _status,
                                }
                            )
                    except Exception:
                        logger.debug("viz journal final event failed", exc_info=True)

                # Review gate: if the issue is already in pending_review
                # (set by the early return above), skip the final status
                # transition so the outer finally does NOT overwrite it with
                # COMPLETED. The human must run `orchestrator issue review
                # --id ... --approve` to move it to COMPLETED.
                if session.issue.id in self._state.pending_review:
                    # Issue is waiting for human review — do nothing further.
                    # Workspace preservation is handled by the early return.
                    logger.info(
                        "Issue %s left in pending_review state — human review required",
                        session.issue.id,
                    )
                elif session.status == "completed":
                    self.status_dashboard.on_session_complete(session.issue.id or "")
                    self._emit_im_event(
                        session.issue.id or "",
                        "issue.completed",
                        EventLevel.SUCCESS,
                        "任务完成",
                        self._session_payload(session),
                    )
                    self._state.completed.add(session.issue.id or "")
                    self._registry.mark_completed(session.issue.id or "")
                    await self._sync_tracker_issue_state(session.issue.id or "", "completed")
                elif session.status == "verification_failed":
                    self.status_dashboard.on_session_failed(
                        session.issue.id or "",
                        str(session.status),
                    )
                    # Terminal IM event already emitted by the originating
                    # except handler (VerificationFailed / HookFailedError /
                    # GitSyncPostCommitError). ``verification_failed`` is
                    # only ever set there, so re-emitting here would double
                    # (e.g. ``post_commit_failed`` ERROR then
                    # ``verification.failed`` WARN). See review 🟡2.
                    self._registry.mark_verification_failed(
                        session.issue.id or "",
                        output=getattr(session, "verification_output", None),
                        hook_error=getattr(session, "last_hook_error", None),
                    )
                    await self._sync_tracker_issue_state(
                        session.issue.id or "", "verification_failed"
                    )
                    await self._schedule_retry(session)
                elif session.status == "agent_timeout":
                    self.status_dashboard.on_session_failed(
                        session.issue.id or "",
                        str(session.status),
                    )
                    # Terminal IM event already emitted by the
                    # ``asyncio.TimeoutError`` except handler; ``agent_timeout``
                    # is only ever set there. See review 🟡2.
                    self._registry.mark_failed_with_reason(
                        session.issue.id or "",
                        getattr(session, "last_hook_error", None)
                        or getattr(session, "verification_output", None)
                        or "Agent run timed out",
                    )
                    await self._sync_tracker_issue_state(session.issue.id or "", "failed")
                    await self._schedule_retry(session)
                elif session.status == "max_turns_exceeded":
                    self.status_dashboard.on_session_failed(
                        session.issue.id or "",
                        str(session.status),
                    )
                    self._emit_im_event(
                        session.issue.id or "",
                        "agent.max_turns_exceeded",
                        EventLevel.WARN,
                        "max turns exceeded",
                    )
                    self._registry.mark_failed(session.issue.id or "")
                    await self._sync_tracker_issue_state(session.issue.id or "", "failed")
                    await self._schedule_retry(
                        session,
                        delay_base_ms=self.workflow.agent.max_turns_retry_delay_ms,
                    )
                elif session.status == "rate_limit_circuit_open":
                    # The AgentRunner's 429 backoff circuit breaker tripped
                    # after ``rate_limit_max_retries`` consecutive rate
                    # limit hits. Surface it on the dashboard and hand it
                    # off to the inter-run retry queue with the longest
                    # configured base delay so the provider's rate window
                    # has a chance to reset before the next attempt.
                    backoff_s = self.workflow.agent.rate_limit_max_backoff_ms
                    logger.warning(
                        "Rate limit circuit open issue_id=%s — scheduling "
                        "inter-run retry with base delay %dms (session "
                        "spent %.1fs in in-turn backoff across %d hits)",
                        session.issue.id or "",
                        backoff_s,
                        getattr(session, "total_429_backoff_seconds", 0.0),
                        getattr(session, "consecutive_429_count", 0),
                    )
                    self.status_dashboard.on_session_failed(
                        session.issue.id or "",
                        "rate_limit_circuit_open",
                    )
                    self._emit_im_event(
                        session.issue.id or "",
                        "agent.rate_limit_circuit_open",
                        EventLevel.ERROR,
                        "rate limit circuit open",
                    )
                    self._registry.mark_failed(session.issue.id or "")
                    await self._sync_tracker_issue_state(session.issue.id or "", "failed")
                    await self._schedule_retry(
                        session,
                        delay_base_ms=backoff_s,
                    )
                elif session.status in (
                    "stagnation",
                    "loop_detected",
                ):
                    # Root-cause fix: the agent loop detected it
                    # was no longer making progress (stagnation =
                    # consecutive no-op turns; loop_detected = same
                    # tool-call signature repeated within window).
                    # Mark the issue failed with the explicit
                    # session_end_reason so the dashboard / cron tick
                    # can distinguish these from ordinary crashes.
                    logger.warning(
                        "Agent %s issue_id=%s — %s: %s",
                        session.status,
                        session.issue.id or "",
                        getattr(session, "session_end_summary", ""),
                    )
                    self.status_dashboard.on_session_failed(
                        session.issue.id or "",
                        str(session.status),
                    )
                    self._emit_im_event(
                        session.issue.id or "",
                        f"agent.{session.status}",
                        EventLevel.WARN,
                        getattr(session, "session_end_summary", "") or str(session.status),
                    )
                    self._registry.mark_failed(session.issue.id or "")
                    await self._sync_tracker_issue_state(session.issue.id or "", "failed")
                    # No retry — same agent will likely repeat the
                    # same loop on retry without human intervention.
                    # The cron tick will mark the issue abandoned on
                    # the next pass and the operator can either
                    # adjust the issue / workflow or skip it.
                elif session.status == "cancelled":
                    logger.info(
                        "Issue %s cancelled by operator — skipping retry",
                        session.issue.id,
                    )
                    self.status_dashboard.on_session_failed(
                        session.issue.id or "",
                        "cancelled",
                    )
                    self._emit_im_event(
                        session.issue.id or "",
                        "issue.cancelled",
                        EventLevel.WARN,
                        "cancelled by operator",
                    )
                    self._registry.mark_failed(session.issue.id or "")
                    await self._sync_tracker_issue_state(session.issue.id or "", "failed")
                    # Do NOT schedule retry — operator explicitly cancelled.
                else:
                    self.status_dashboard.on_session_failed(
                        session.issue.id or "",
                        str(session.status),
                    )
                    # Use the error detail (session_end_summary) as the
                    # message if available — str(session.status) is just
                    # "failed" with no context.  Truncate long error
                    # bodies (e.g. API JSON responses) for WeChat display.
                    detail = getattr(session, "session_end_summary", None) or str(session.status)
                    if len(detail) > 200:
                        detail = detail[:200] + "…"
                    self._emit_im_event(
                        session.issue.id or "",
                        "issue.failed",
                        EventLevel.WARN,
                        detail,
                        self._session_payload(
                            session,
                            turns=getattr(session, "turn_count", None),
                        ),
                    )
                    failure_detail = getattr(session, "operator_failure_detail", None)
                    if failure_detail:
                        self._registry.mark_failed_with_reason(
                            session.issue.id or "",
                            str(failure_detail),
                        )
                        self._registry.update_report(
                            session.issue.id or "",
                            session_end_reason=getattr(session, "session_end_reason", None),
                            session_end_summary=getattr(session, "session_end_summary", ""),
                        )
                    else:
                        self._registry.mark_failed(session.issue.id or "")
                    await self._sync_tracker_issue_state(session.issue.id or "", "failed")
                    # Schedule retry
                    await self._schedule_retry(session)

                # Update summary comment for non-completed paths
                if session.issue.id not in self._state.pending_review:
                    await self._update_issue_summary(session)

                # Cleanup workspace based on preservation policy
                try:
                    issue_record = self._registry.get(session.issue.id)
                    await self.workspace.cleanup(
                        session.issue,
                        end_status=getattr(session, "status", None),
                        end_reason=getattr(session, "session_end_reason", None),
                        agent_config=getattr(self, "_agent_config", None),
                        issue_record=issue_record,
                    )
                except Exception as exc:
                    logger.warning(
                        "Workspace cleanup failed issue_id=%s: %s",
                        session.issue.id,
                        exc,
                    )

                self._state.claimed.discard(session.issue.id or "")

    async def _update_issue_summary(self, session: AgentSession) -> None:
        """Update the issue summary comment with final status for failure paths."""
        comment_id = getattr(session, "summary_comment_id", None)
        body_lines = [
            "## Orchestratord Run Summary",
            "",
            f"- Run: `{getattr(session, 'run_id', 'unknown')}`",
            f"- Status: `{getattr(session, 'status', 'unknown')}`",
            f"- Turns: {getattr(session, 'turn_count', 0)}",
            f"- Tool calls: {getattr(session, 'tool_count', 0)}",
        ]
        # Three-level fallback: reason → summary → hook_error
        end_reason = getattr(session, "session_end_reason", None)
        end_summary = getattr(session, "session_end_summary", None)
        hook_error = getattr(session, "last_hook_error", None)
        reason_text = end_reason or end_summary or hook_error
        if reason_text:
            body_lines.append(f"- Error: `{reason_text}`")
        if hook_error and hook_error != reason_text:
            body_lines.append(f"- Detail: `{hook_error}`")
        # User-facing guidance — only for FAILURE paths. A successful end
        # reason (e.g. "success") is not in the failure guidance table, so
        # it would fall through to the generic "未知错误" fallback and
        # produce a self-contradictory comment ("Status: completed" +
        # "失败原因：未知错误"). Skip guidance entirely for success.
        if end_reason and end_reason not in _SUCCESS_END_REASONS:
            guidance = end_reason_guidance(
                end_reason=end_reason,
                hook_error=hook_error,
                end_summary=end_summary,
                output_text=getattr(session, "output_text", None),
            )
            if guidance:
                readable, action = guidance
                body_lines.append("")
                body_lines.append(f"失败原因：{readable}")
                body_lines.append(f"建议操作：{action}")
        body = "\n".join(body_lines)
        try:
            if comment_id is None:
                # No summary comment exists yet — create one so failures
                # always surface feedback on the issue. Previously a missing
                # summary_comment_id silently dropped failure feedback.
                new_id = await self.tracker.create_comment(
                    session.issue.id or "", body
                )
                session.summary_comment_id = new_id
            else:
                await self.tracker.update_comment(session.issue.id, comment_id, body)
        except Exception as exc:
            logger.warning(
                "Failed to update summary comment issue_id=%s: %s", session.issue.id, exc
            )

    async def _apply_review_rules(self, session: AgentSession) -> None:
        """确保 review commit 包含 review metadata。

        规则提取已从 follow-up 流水线中移除，改为 CLI 命令
        ``orchestratord rules extract`` 手动触发。
        Commit message 中已由 ``GitSyncService`` 写入 review
        metadata（review-pr / review-id），供 CLI extract 命令
        扫描 commit log 时解析。
        """
        pass

    async def _reply_to_processed_feedback(self, session: AgentSession) -> None:
        if not self.workflow.review_feedback.reply_to_comments:
            return
        pull_request = getattr(session, "pull_request", None)
        feedback_ids = set(getattr(session, "feedback_ids", []))
        if pull_request is None or not feedback_ids:
            return
        if not supports(self.tracker, PullRequestFeedbackCapability):
            return
        try:
            feedback = await self.tracker.fetch_pull_request_feedback(
                pull_request=pull_request,
                issue_id=session.issue.id,
                include_ci_failures=False,
            )
        except Exception as exc:
            logger.warning(
                "Failed to refresh feedback for replies issue_id=%s: %s", session.issue.id, exc
            )
            return
        from .review_feedback import REPLY_MARKER

        body = REPLY_MARKER
        for item in feedback:
            if item.id not in feedback_ids:
                continue
            try:
                await self.tracker.reply_to_pull_request_feedback(
                    pull_request=pull_request,
                    feedback=item,
                    body=body,
                    issue_id=session.issue.id,
                )
            except Exception as exc:
                logger.warning(
                    "Failed to reply to PR feedback issue_id=%s feedback_id=%s: %s",
                    session.issue.id,
                    item.id,
                    exc,
                )

    async def _post_feedback_summary(self, session: AgentSession, sync_result: Any) -> None:
        """Post a processing summary comment to the PR after a review follow-up."""
        pull_request = getattr(session, "pull_request", None)
        feedback_ids = list(getattr(session, "feedback_ids", []))
        if pull_request is None or not feedback_ids:
            return
        record = self._registry.get(session.issue.id or "")
        attempt = record.followup_attempt_count if record else 1

        if not supports(self.tracker, PullRequestFeedbackCapability):
            return
        try:
            all_feedback = await self.tracker.fetch_pull_request_feedback(
                pull_request=pull_request,
                issue_id=session.issue.id,
                include_ci_failures=self.workflow.review_feedback.include_ci_failures,
            )
        except Exception as exc:
            logger.warning(
                "Failed to fetch feedback for summary issue_id=%s: %s", session.issue.id, exc
            )
            all_feedback = []

        feedback_by_id = {item.id: item for item in all_feedback}
        processed = []
        skipped = []
        commit_sha = getattr(sync_result, "commit_sha", None)
        for fid in feedback_ids:
            fb = feedback_by_id.get(fid)
            if fb is None:
                continue
            if commit_sha:
                processed.append(fb)
            else:
                skipped.append({"feedback": fb, "reason": "No changes were committed"})

        summary = PromptBuilder.render_feedback_summary(
            attempt=attempt,
            processed=processed,
            skipped=skipped,
        )
        try:
            await self.tracker.create_comment(session.issue.id or "", summary)
        except Exception as exc:
            logger.warning("Failed to post feedback summary issue_id=%s: %s", session.issue.id, exc)

    async def _handle_review_followup_control(self, issue_id: str, extra: str) -> None:
        """Handle a CLI-approved review_followup control command."""
        if not issue_id:
            return
        record = self._registry.get(issue_id)
        if record is None:
            logger.warning("review_followup control: issue %s not in registry", issue_id)
            return
        if issue_id in self._state.running:
            logger.info("review_followup control: issue %s already running, skipping", issue_id)
            return

        feedback_ids = (
            [fid.strip() for fid in extra.split(",") if fid.strip()]
            if extra
            else list(record.pending_feedback_ids)
        )
        if not feedback_ids:
            logger.info("review_followup control: no feedback IDs for issue %s", issue_id)
            return

        pull_request = PullRequestRef(
            number=record.pr_number,
            url=record.pr_url,
        )
        issue = Issue(
            id=record.issue_id,
            identifier=record.issue_identifier,
            title=record.issue_identifier,
            branch_name=record.branch_name,
        )
        feedback_items: list[PullRequestFeedback] = []
        if not supports(self.tracker, PullRequestFeedbackCapability):
            return
        try:
            all_feedback = await self.tracker.fetch_pull_request_feedback(
                pull_request=pull_request,
                issue_id=record.issue_id,
                include_ci_failures=self.workflow.review_feedback.include_ci_failures,
            )
            feedback_by_id = {item.id: item for item in all_feedback}
            for fid in feedback_ids:
                if fid in feedback_by_id:
                    feedback_items.append(feedback_by_id[fid])
        except Exception as exc:
            logger.error(
                "review_followup control: failed to fetch feedback for issue %s: %s", issue_id, exc
            )
            return

        if not feedback_items:
            logger.info(
                "review_followup control: no matching feedback found for issue %s", issue_id
            )
            return

        followup = ReviewFollowup(
            issue=issue,
            record=record,
            pull_request=pull_request,
            feedback=feedback_items,
            prompt="",
        )
        self._state.claimed.add(issue_id)
        await self._launch_review_followup(followup)
        logger.info(
            "review_followup control: launched follow-up for issue %s with %d feedback items",
            issue_id,
            len(feedback_items),
        )

    async def _schedule_retry(
        self,
        session: AgentSession,
        *,
        delay_base_ms: int | None = None,
    ) -> None:
        """Schedule a retry for a failed session.

        ``delay_base_ms`` overrides the base delay for the exponential backoff
        curve. When ``None`` the default ``_FAILURE_RETRY_BASE_MS`` is used
        (10s). The orchestrator passes ``workflow.agent.max_turns_retry_delay_ms``
        for ``max_turns_exceeded`` sessions so the longer wait default kicks in
        without forcing all retries to share it.
        """
        issue_id = session.issue.id or ""

        # Operator-initiated stops must never be defeated by the
        # auto-retry loop (stop → retry → stop burning API calls until
        # max attempts). These end reasons are terminal; the operator
        # can run the issue again explicitly via ``issue retry``.
        end_reason = getattr(session, "session_end_reason", None)
        if end_reason in _NON_RETRYABLE_END_REASONS:
            logger.warning(
                "Not auto-retrying issue_id=%s — end reason '%s' is "
                "operator-initiated; use 'issue retry' to run again",
                issue_id,
                end_reason,
            )
            self._state.claimed.discard(issue_id)
            return

        attempt = self._state.retry_attempts.get(issue_id, 0) + 1
        self._state.retry_attempts[issue_id] = attempt

        # Retry context: persist the just-failed run_id so the next
        # attempt's agent can Read() the previous transcript to understand
        # what was tried and where it failed.
        if session.run_id:
            record = self._registry.get(issue_id)
            if record is not None:
                if record.previous_run_ids is None:
                    record.previous_run_ids = []
                if session.run_id not in record.previous_run_ids:
                    record.previous_run_ids.append(session.run_id)
                    self._registry._save()

        max_attempts = self.workflow.agent.max_retry_attempts
        if max_attempts and attempt > max_attempts:
            logger.warning(
                "Retry limit reached issue_id=%s attempts=%d max=%d — giving up",
                issue_id,
                attempt,
                max_attempts,
            )
            self._state.claimed.discard(issue_id)
            self._registry.mark_abandoned(issue_id)
            await self._sync_tracker_issue_state(issue_id, "abandoned")
            return

        # Exponential backoff capped at max_retry_backoff_ms
        base_ms = delay_base_ms if delay_base_ms is not None else _FAILURE_RETRY_BASE_MS
        max_ms = self.workflow.agent.max_retry_backoff_ms
        delay_ms = min(base_ms * (1 << (attempt - 1)), max_ms)

        retry = RetryItem(
            issue_id=issue_id,
            attempt=attempt,
            delay_seconds=delay_ms / 1000.0,
            identifier=session.issue.identifier or "",
            error=f"agent failed: {session.status}",
        )
        self._state.retry_queue.append(retry)
        # Persist the retry plan on the registry record so a
        # daemon restart cannot silently drop a waiting retry and
        # operators can see why nothing is running.
        record = self._registry.get(issue_id)
        if record is not None:
            record.retry_count = attempt
            record.next_retry_at = retry.scheduled_at + retry.delay_seconds
            record.touch()
            self._registry._save()
        logger.info(
            "Scheduled retry issue_id=%s attempt=%s delay=%sms",
            issue_id,
            attempt,
            delay_ms,
        )
        self._emit_im_event(
            issue_id,
            "intent.retry",
            EventLevel.INFO,
            f"retry scheduled in {delay_ms}ms",
            {"attempt": attempt, "delay_ms": delay_ms},
        )

    def _broadcast_clarification_status(self) -> None:
        """收集所有 issue 的澄清状态，推送到 dashboard。"""
        if self.status_dashboard is None:
            return
        from .status_dashboard import ClarificationEntry

        now = time.time()
        max_rounds = getattr(
            getattr(self.workflow, "clarifier", None),
            "max_rounds",
            2,
        )
        entries: list[ClarificationEntry] = []
        for issue_id, record in self._registry._records.items():
            status = record.clarification_status
            if status in ("awaiting_author", "awaiting_local", "manual_required", "resolved"):
                elapsed = now - (record.updated_at or now)
                entries.append(
                    ClarificationEntry(
                        issue_id=issue_id,
                        status=status or "",
                        open_questions=list(record.open_questions),
                        round_num=record.clarification_round,
                        max_rounds=max_rounds,
                        elapsed_seconds=elapsed,
                        author_login=record.author_login,
                    )
                )
        self.status_dashboard.on_clarification_update(entries)

    def _compute_workspace_focus_for_clarifier(self, issue: "Issue") -> list[dict]:
        """计算 workspace focus 作为澄清上下文富化。

        仅在 follow-up 分支已建时调用。新 issue 场景（分支未建）返回 []。
        """
        branch = getattr(issue, "branch_name", None) or getattr(issue, "linked_branch", None)
        if not branch:
            return []
        try:
            changed = self._git_changed_files(branch)
            if not changed:
                return []
            from .workspace_focus import compute_workspace_focuses

            return compute_workspace_focuses(changed_files=changed, recent_messages=[])
        except Exception as exc:
            logger.warning("Workspace focus computation failed for issue %s: %s", issue.id, exc)
            return []

    async def _process_escalated_issues(self) -> None:
        """Check for clarification-exhausted issues and apply escalation policy.

        When a clarification item is marked EXHAUSTED, the escalation policy
        determines what happens next:
          - skip: mark as ABANDONED so orchestrator skips it on next poll
          - mark_failed: mark as FAILED
          - notify: mark as FAILED + send notification
        """
        import json

        sentinel_path = self._workspace_root / ".escalated_issues.json"
        if not sentinel_path.exists():
            return

        try:
            data = json.loads(sentinel_path.read_text())
        except Exception:
            return

        if not data:
            return

        # Collect IDs to remove from sentinel
        to_remove = []

        for issue_id in data:
            if issue_id in self._state.completed or issue_id in self._state.claimed:
                to_remove.append(issue_id)
                continue

            policy = self._clarification_resolver._config.escalation
            if policy == "mark_failed":
                self._registry.mark_failed(issue_id)
                await self._sync_tracker_issue_state(issue_id, "failed")
                self._state.completed.add(issue_id)
                self._emit_im_event(
                    issue_id,
                    "clarification.exhausted",
                    EventLevel.WARN,
                    "clarification exhausted",
                )
            elif policy == "notify":
                self._registry.mark_failed(issue_id)
                await self._sync_tracker_issue_state(issue_id, "failed")
                self._state.completed.add(issue_id)
                logger.warning("Escalation notify for issue %s", issue_id)
                self._emit_im_event(
                    issue_id,
                    "clarification.exhausted",
                    EventLevel.ERROR,
                    "clarification exhausted",
                )
            else:  # skip → mark as abandoned
                self._registry.mark_abandoned(issue_id)
                await self._sync_tracker_issue_state(issue_id, "abandoned")
                self._state.completed.add(issue_id)
                logger.info("Escalation skip for issue %s", issue_id)
                self._emit_im_event(
                    issue_id,
                    "clarification.exhausted",
                    EventLevel.WARN,
                    "clarification exhausted",
                )

            to_remove.append(issue_id)

        # Prune processed entries from sentinel
        if to_remove:
            for issue_id in to_remove:
                data.pop(issue_id, None)
            sentinel_path.write_text(json.dumps(data, indent=2))

    def _retry_requeue_limit(self) -> int:
        """Ceiling for tracker-miss requeues.

        A retry that the tracker permanently fails to report must not
        loop forever; the cap mirrors ``max_retry_attempts``.
        """
        return max(1, int(getattr(self.workflow.agent, "max_retry_attempts", 5) or 5))

    def _requeue_retry(self, retry: Any, now: float) -> bool:
        """Re-queue a retry with a doubled, capped delay.

        Returns ``False`` when the requeue ceiling is exhausted — the
        item is dropped (with a warning) and the persisted plan cleared.
        """
        retry.requeue_count = getattr(retry, "requeue_count", 0) + 1
        if retry.requeue_count > self._retry_requeue_limit():
            logger.warning(
                "Retry issue %s exceeded the requeue ceiling (%d) — "
                "dropping the persisted retry plan; use 'issue retry' "
                "to run again",
                retry.issue_id,
                self._retry_requeue_limit(),
            )
            self._clear_retry_plan(retry.issue_id)
            return False
        retry.delay_seconds = min(
            retry.delay_seconds * 2,
            self.workflow.agent.max_retry_backoff_ms / 1000.0,
        )
        retry.scheduled_at = now
        self._state.retry_queue.append(retry)
        return True

    def _clear_retry_plan(self, issue_id: str) -> None:
        """Clear ``next_retry_at`` on the registry record (best-effort)."""
        record = self._registry.get(issue_id)
        if record is not None and record.next_retry_at is not None:
            record.next_retry_at = None
            record.touch()
            self._registry._save()

    async def _process_retry_queue(self) -> None:
        """Process retry queue with exponential backoff.

        Retries are processed before new candidate issues so that
        previously-failed work gets priority.
        """
        import time

        now = time.time()
        ready: list[Any] = []
        not_ready: list[Any] = []

        for retry in self._state.retry_queue:
            if now >= retry.scheduled_at + retry.delay_seconds:
                ready.append(retry)
            else:
                not_ready.append(retry)

        # Explicit reassembly — deferred items are appended to
        # ``not_ready`` below and the queue is rewritten afterwards, so
        # retention no longer depends on ``remaining`` aliasing the live
        # queue list.
        self._state.retry_queue = not_ready

        for retry in ready:
            # Skip if already running or completed
            if retry.issue_id in self._state.running or retry.issue_id in self._state.completed:
                logger.debug("Retry skipped issue_id=%s already running/completed", retry.issue_id)
                continue

            # Check concurrency slot
            if len(self._state.running) >= self._state.max_concurrent_agents:
                logger.debug("Retry deferred issue_id=%s no concurrency slots", retry.issue_id)
                self._state.retry_queue.append(retry)
                continue

            # Re-fetch issue state from tracker
            try:
                issues = await self.tracker.fetch_issue_states_by_ids([retry.issue_id])
                issue = issues.get(retry.issue_id)
                if issue is None:
                    # A fetch that omits the issue is not a reason
                    # to silently drop the retry — re-queue with an
                    # extended delay so transient tracker paginations /
                    # API hiccups cannot lose the plan.
                    # a *permanently* vanished issue would otherwise loop
                    # forever, so the requeue carries an attempt ceiling.
                    requeued = self._requeue_retry(retry, now)
                    if requeued:
                        logger.warning(
                            "Retry issue %s missing from tracker fetch — "
                            "re-queued (requeue %d/%d)",
                            retry.issue_id,
                            retry.requeue_count,
                            self._retry_requeue_limit(),
                        )
                    continue
            except Exception as exc:
                logger.error("Failed to fetch retry issue %s: %s", retry.issue_id, exc)
                self._requeue_retry(retry, now)
                continue

            # Check if issue is still in active states
            active_states = [
                s.strip().lower() for s in (getattr(self.tracker, "active_states", None) or [])
            ]
            if issue.state and issue.state.strip().lower() not in active_states:
                logger.warning(
                    "Retry issue %s no longer active (state=%s), dropping "
                    "the persisted retry plan",
                    retry.issue_id,
                    issue.state,
                )
                self._clear_retry_plan(retry.issue_id)
                continue

            self._state.claimed.add(retry.issue_id)
            # The plan is being executed — clear next_retry_at so
            # the registry reflects reality.
            self._clear_retry_plan(retry.issue_id)
            await self._launch_issue(issue)
            logger.info(
                "Retry launched issue_id=%s attempt=%s",
                retry.issue_id,
                retry.attempt,
            )

    async def _process_control_commands(self) -> None:
        """Process lifecycle control commands from CLI.

        Checks the control directory for pause/resume/stop/takeover commands
        written by the orchestrator control CLI.
        """
        import os

        control_dir = self._workspace_root / ".orchestrator_control"
        if not control_dir.exists():
            return

        try:
            for control_file in control_dir.iterdir():
                if not control_file.name.endswith(".control"):
                    continue
                parts = control_file.read_text(encoding="utf-8").strip().split("\n")
                if not parts:
                    continue
                cmd = parts[0].strip()
                issue_id = parts[1].strip() if len(parts) > 1 else ""
                extra = "\n".join(parts[2:]).strip() if len(parts) > 2 else ""

                try:
                    if cmd == "review_followup":
                        await self._handle_review_followup_control(issue_id, extra)
                    elif cmd == "rebase":
                        # Route CLI-written rebase control files to
                        # the built-in rebase path. Format::
                        #   rebase\n<id>\nforce=0|1\n<reason>
                        await self._handle_rebase_control(issue_id, extra)
                    elif cmd in {"gateway_connect", "gateway_disconnect"}:
                        await self._handle_gateway_control(cmd, extra)
                    elif cmd == "review_approve":
                        await self._handle_review_approve_control(issue_id, extra)
                    elif cmd == "review_retry":
                        await self._handle_review_retry_control(issue_id, extra)
                    elif cmd == "retry":
                        await self._handle_retry_control(issue_id, extra)
                    elif cmd == "followup":
                        await self._handle_followup_control(issue_id, extra)
                    else:
                        self._apply_control_command(cmd, issue_id, extra)
                finally:
                    # Clean up control file after processing
                    try:
                        control_file.unlink()
                    except Exception:
                        pass
        except Exception as exc:
            logger.warning("Failed to process control commands: %s", exc)

    async def _handle_gateway_control(self, cmd: str, extra: str) -> None:
        """Handle CLI-written IM gateway connect/disconnect control files."""
        payload: dict[str, Any] = {}
        if extra:
            try:
                payload = json.loads(extra)
            except json.JSONDecodeError as exc:
                logger.warning("gateway control: invalid payload: %s", exc)
                return
        response_path = payload.get("response_path")
        if cmd == "gateway_connect":
            result = await self._connect_gateway_runtime(
                origin=str(payload.get("origin") or ""),
                sock=str(payload.get("sock") or ""),
            )
        else:
            result = await self._disconnect_gateway_runtime()
        if response_path:
            self._write_gateway_control_result(Path(str(response_path)), result)

    def _write_gateway_control_result(self, path: Path, result: dict[str, Any]) -> None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
        except Exception:  # noqa: BLE001
            logger.debug("gateway control: failed to write result %s", path, exc_info=True)

    async def _connect_gateway_runtime(self, *, origin: str, sock: str) -> dict[str, Any]:
        if not origin:
            return {"ok": False, "message": "gateway origin is required"}
        if not sock:
            return {"ok": False, "message": "gateway socket is required"}

        current = getattr(self, "_im_gateway_wrapper", None)
        current_ipc = getattr(current, "_ipc", None)
        current_origin = getattr(current, "_origin", None)
        current_sock = str(getattr(current_ipc, "socket_path", getattr(current_ipc, "sock", "")))
        if current is not None and current_origin == origin and current_sock == sock:
            return {"ok": True, "message": "already connected"}

        from .ipc.client import GatewayIpcClient
        from .im_gateway_client import (
            OrchestratorGatewayClient,
            OrchestratorHandlers,
        )

        def _control_verb(verb, issue_id):
            self._apply_control_command(verb, issue_id or "", "")

        def _issue_inject(issue_id, hint):
            hints_file = self._workspace_root / ".operator_hints.md"
            hints_file.parent.mkdir(parents=True, exist_ok=True)
            with hints_file.open("a", encoding="utf-8") as f:
                f.write(f"\n{hint}\n")

        handlers = OrchestratorHandlers(
            queue_pending_message=lambda issue_id, text: logger.info(
                "IM followup queued: issue=%s text_len=%d", issue_id, len(text)
            ),
            control_verb=_control_verb,
            issue_inject=_issue_inject,
            operator_hints=_issue_inject,
            agent_intent=_control_verb,
            issue_cli=lambda verb, issue_id, payload: logger.info(
                "IM issue_cli: %s issue=%s", verb, issue_id
            ),
            bridge_interrupt=lambda issue_id, payload: _control_verb("stop", issue_id),
        )
        session_id = f"orchestrator-{os.getpid()}-{int(time.time() * 1000)}"
        ipc = GatewayIpcClient(sock, instance_id=session_id)
        wrapper = OrchestratorGatewayClient(
            handlers, ipc_client=ipc, origin=origin, command_router=None, control_bridge=None
        )
        try:
            await ipc.connect()
            response = await ipc.register(
                session_id=session_id,
                origin=origin,
                capabilities=["outbound_text"],
            )
            if response is None or response.ack_layer != "accepted":
                await ipc.close()
                return {"ok": False, "message": "gateway registration failed"}
        except FileNotFoundError:
            await ipc.close()
            return {"ok": False, "message": "IM gateway daemon is not running"}
        except Exception as exc:  # noqa: BLE001
            await ipc.close()
            logger.warning("gateway control connect failed", exc_info=True)
            return {"ok": False, "message": str(exc)}

        old_wrapper = getattr(self, "_im_gateway_wrapper", None)
        old_task = getattr(self, "_im_gateway_heartbeat_task", None)
        old_session_id = getattr(self, "_im_gateway_session_id", None)
        deliver = self._build_gateway_ipc_deliver(wrapper)
        self._im_gateway_wrapper = wrapper
        self._im_gateway_session_id = session_id
        self._im_gateway_heartbeat_task = asyncio.create_task(
            self._gateway_runtime_heartbeat_loop(wrapper, session_id)
        )
        self.im_event_deliver = deliver
        self.im_event_channel = "wechat"
        self._attach_gateway_sink_to_existing_emitters(deliver)
        if callable(getattr(wrapper, "_flush_pending_outbound", None)):
            await wrapper._flush_pending_outbound()
        if old_task is not None and not old_task.done():
            old_task.cancel()
            with __import__("contextlib").suppress(asyncio.CancelledError):
                await old_task
        if old_wrapper is not None and old_wrapper is not wrapper:
            await self._close_gateway_wrapper(old_wrapper, old_session_id)
        self._emit_im_event(
            "",
            "orchestrator.started",
            EventLevel.INFO,
            "IM notifications enabled",
        )
        return {"ok": True, "message": "connected"}

    def _build_gateway_ipc_deliver(self, wrapper) -> Any:
        loop = asyncio.get_running_loop()

        def _sync_deliver(event, text):
            loop.create_task(wrapper.send_outbound(text))

        return _sync_deliver

    def _attach_gateway_sink_to_existing_emitters(self, deliver) -> None:
        emitters = getattr(self, "_im_emitters", {}) or {}
        if not emitters:
            return
        from .sinks.channel import ChannelProgressSink

        for emitter in list(emitters.values()):
            add_sink = getattr(emitter, "add_sink", None)
            if callable(add_sink):
                add_sink(ChannelProgressSink(deliver))

    async def _gateway_runtime_heartbeat_loop(self, wrapper, session_id: str) -> None:
        ipc = getattr(wrapper, "_ipc", None)
        if ipc is None:
            return
        while True:
            try:
                await ipc.heartbeat()
            except Exception:  # noqa: BLE001
                logger.debug("orchestrator IM runtime heartbeat failed", exc_info=True)
            await asyncio.sleep(30.0)

    async def _disconnect_gateway_runtime(self) -> dict[str, Any]:
        wrapper = getattr(self, "_im_gateway_wrapper", None)
        task = getattr(self, "_im_gateway_heartbeat_task", None)
        session_id = getattr(self, "_im_gateway_session_id", None)
        if task is not None and not task.done():
            task.cancel()
            with __import__("contextlib").suppress(asyncio.CancelledError):
                await task
        if wrapper is not None:
            await self._close_gateway_wrapper(wrapper, session_id)
        self._im_gateway_wrapper = None
        self._im_gateway_heartbeat_task = None
        self._im_gateway_session_id = None
        self.im_event_deliver = None
        self.im_event_channel = ""
        return {"ok": True, "message": "disconnected"}

    async def _close_gateway_wrapper(self, wrapper, session_id: str | None) -> None:
        ipc = getattr(wrapper, "_ipc", None)
        if ipc is None:
            return
        with __import__("contextlib").suppress(RuntimeError, ConnectionError, OSError):
            await ipc.unregister(session_id)
        await ipc.close()

    def _reset_issue_for_retry(
        self,
        issue_id: str,
        feedback: str,
        *,
        intent: Intent = Intent.RETRY,
        reset_retry_count: bool = False,
        command: str | None = None,
    ) -> bool:
        """Reset review-gated state and queue feedback without requiring a running session."""
        if not issue_id:
            return False

        record = self._registry._records.get(issue_id)
        is_known = bool(
            record
            or issue_id in self._state.running
            or issue_id in self._state.pending_review
            or issue_id in self._state.completed
            or issue_id in self._state.claimed
        )
        if not is_known:
            logger.debug("Retry control for unknown issue %s", issue_id)
            return False

        if feedback:
            question = f"[Human Review Rejected] {feedback}"
            self._clarification_queue.inject_feedback(issue_id, question)

        self._state.pending_review.discard(issue_id)
        self._state.completed.discard(issue_id)
        self._state.claimed.discard(issue_id)
        failed = getattr(self._state, "failed", None)
        if failed is not None:
            failed.discard(issue_id)
        retry_attempts = getattr(self._state, "retry_attempts", None)
        if retry_attempts is not None:
            retry_attempts.pop(issue_id, None)
        retry_queue = getattr(self._state, "retry_queue", None)
        if retry_queue is not None:
            self._state.retry_queue = [retry for retry in retry_queue if retry.issue_id != issue_id]
        if record:
            was_pending_review = record.status is IssueStatus.PENDING_REVIEW
            record.status = IssueStatus.PENDING
            record.intent = intent
            record.intent_source = "cli"
            if reset_retry_count:
                record.retry_count = 0
            if command is not None:
                record.last_command = command
            elif feedback:
                record.last_command = "/issue review --reject"
            if was_pending_review:
                record.attempt_count += 1
            record.touch()
            self._registry._save()

        logger.info(
            "Issue %s queued for retry (attempt %d)",
            issue_id,
            record.attempt_count if record else 1,
        )
        self._emit_im_event(issue_id, "intent.retry", EventLevel.INFO, "retry requested")
        return True

    async def _handle_retry_control(self, issue_id: str, reason: str) -> None:
        """Apply a durable retry request and make the tracker eligible for polling."""
        if not self._reset_issue_for_retry(
            issue_id,
            "",
            reset_retry_count=True,
            command=f"cli:reset:{reason[:64]}",
        ):
            return
        await self._sync_tracker_issue_state(issue_id, "open")

    async def _handle_followup_control(self, issue_id: str, extra: str) -> None:
        """Re-launch a completed issue with FOLLOWUP intent.

        Unified handler for both CLI ``--mode followup`` and chat follow-up.
        Unlike ``_handle_retry_control`` this does NOT reset the PR or
        branch — the agent reuses the existing branch and appends a
        follow-up commit.  The follow-up prompt text is read from
        ``.operator_hints.md`` by ``prompt_builder`` at launch time.
        """
        if not issue_id:
            return
        record = self._registry._records.get(issue_id)
        is_known = bool(
            record
            or issue_id in self._state.running
            or issue_id in self._state.pending_review
            or issue_id in self._state.completed
            or issue_id in self._state.claimed
        )
        if not is_known:
            logger.debug("chat_followup control for unknown issue %s", issue_id)
            return

        # Clear daemon state so the issue is re-eligible for polling.
        self._state.completed.discard(issue_id)
        self._state.claimed.discard(issue_id)
        self._state.pending_review.discard(issue_id)
        failed = getattr(self._state, "failed", None)
        if failed is not None:
            failed.discard(issue_id)
        retry_queue = getattr(self._state, "retry_queue", None)
        if retry_queue is not None:
            self._state.retry_queue = [
                r for r in retry_queue if r.issue_id != issue_id
            ]

        if record:
            record.status = IssueStatus.PENDING
            record.intent = Intent.FOLLOWUP
            record.intent_source = "chat"
            record.last_command = f"chat:followup:{extra[:64]}"
            record.touch()
            self._registry._save()

        logger.info(
            "Issue %s queued for chat follow-up (intent=FOLLOWUP)",
            issue_id,
        )
        await self._sync_tracker_issue_state(issue_id, "open")

    async def _handle_review_retry_control(self, issue_id: str, feedback: str) -> None:
        """Queue a rejected review as a follow-up that preserves the existing PR."""
        if not self._reset_issue_for_retry(issue_id, feedback, intent=Intent.FOLLOWUP):
            return
        await self._sync_tracker_issue_state(issue_id, "open")

    async def _handle_review_approve_control(self, issue_id: str, comment: str) -> None:
        """Finalize a human approval in registry, daemon state, and remote tracker."""
        record = self._registry.get(issue_id)
        if record is None:
            logger.warning("Review approval ignored for unknown issue %s", issue_id)
            return

        already_completed = record.status is IssueStatus.COMPLETED
        self._registry.mark_completed(issue_id)
        self._state.pending_review.discard(issue_id)
        self._state.claimed.discard(issue_id)
        self._state.completed.add(issue_id)
        tracker_synced = await self._sync_tracker_issue_state(issue_id, "completed")

        if comment and not already_completed:
            try:
                await self.tracker.create_comment(issue_id, f"## Approved\n\n{comment}")
            except Exception as exc:
                logger.warning("Failed to post approval comment issue_id=%s: %s", issue_id, exc)

        if tracker_synced:
            self._emit_im_event(
                issue_id,
                "issue.completed",
                EventLevel.SUCCESS,
                "人工审批通过",
                {
                    "pr": record.pr_url,
                    "branch": record.branch_name,
                    "commit": record.commit_sha,
                },
            )
        else:
            self._emit_im_event(
                issue_id,
                "issue.failed",
                EventLevel.ERROR,
                "审批已记录，但远端 completed 状态同步失败",
                {"pr": record.pr_url},
            )

    def _apply_control_command(self, cmd: str, issue_id: str, extra: str) -> None:
        """Apply a control command, including retries outside running sessions."""
        if cmd == "retry":
            if not self._reset_issue_for_retry(issue_id, extra):
                return
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                return
            loop.create_task(self._sync_tracker_issue_state(issue_id, "open"))
            return

        if not issue_id or issue_id not in self._state.running:
            logger.debug("Control %s for unknown issue %s", cmd, issue_id)
            return

        session = self._state.running[issue_id]
        if cmd == "pause":
            _apply_pause_session(session, extra or "operator requested pause")
            logger.info("Paused issue %s: %s", issue_id, session.pause_reason)
            self._emit_im_event(issue_id, "control.pause", EventLevel.INFO, session.pause_reason)
            # Persist paused state to the registry.
            self._registry.mark_paused(issue_id, reason=session.pause_reason)
        elif cmd == "resume":
            _apply_resume_session(session)
            logger.info("Resumed issue %s", issue_id)
            self._emit_im_event(issue_id, "control.resume", EventLevel.INFO, "resumed")
            # Restore running state in the registry.
            self._registry.mark_resumed(issue_id)
        elif cmd == "stop":
            # Request cancellation via task cancel
            logger.info("Stop requested for issue %s", issue_id)
            session.status = "failed"
            session.pause_resume_event.set()  # Unblock if paused
            self._emit_im_event(issue_id, "control.stop", EventLevel.WARN, "stop requested")
            # Root-cause fix: cancel the asyncio task so the
            # CancelledError handler in _run_issue fires immediately
            # instead of leaving the agent running until the next
            # session end check.
            task = self._issue_tasks.get(issue_id)
            if task is not None and not task.done():
                task.cancel()
                logger.info("Cancelled task for issue %s", issue_id)
        elif cmd == "takeover":
            logger.info("Takeover requested for issue %s", issue_id)
            session.status = "failed"
            session.pause_resume_event.set()  # Unblock if paused
            self._emit_im_event(issue_id, "control.takeover", EventLevel.WARN, "takeover requested")
            # Note: REPL takeover requires full session context - handled separately

    def get_event_stream(self, issue_id: str) -> "asyncio.Queue | None":
        """Get the event queue for a running issue session (for CLI tail)."""
        session = self._state.running.get(issue_id)
        if session is None:
            return None
        return session.event_queue

    async def _cancel_all_tasks(self) -> None:
        """Cancel all running agent tasks."""
        if self._tasks:
            for task in self._tasks:
                task.cancel()
            await asyncio.gather(*self._tasks, return_exceptions=True)
            self._tasks.clear()
