"""Polling engine — agent-agnostic workflow engine.

Orchestrator — agent-agnostic workflow engine.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shlex
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .backend_runner import BackendRunner
from .agent.task import AgentTask, AgentTaskResult
from .agent.runner import AgentTaskRunner
from .git.utils import (
    get_default_branch,
)
from .session_state import AgentSession, RetryItem
from .runner_utils import _apply_pause_session, _apply_resume_session
from .config.schema import WorkflowConfig
from .events import EventLevel
from .git.sync import (
    GitSyncService,
    rebase_for_pr,
)
from .issue_registry.issue import Issue
from .issue_registry import IssueRegistry, IssueStatus
from .kernel.dispatch import (
    FAILURE_RETRY_BASE_MS,
    NON_RETRYABLE_END_REASONS,
    OrchestratorState,
    compute_retry_delay,
    requeue_retry_item,
    split_ready_retries,
)
from .kernel.events import KernelHooks
from .kernel.kernel import OrchestrationKernel
from .kernel.mode_dispatcher import build_mode_selector, register_collaboration_modes
from .kernel.sink_router import attach_asciicast_sink, build_base_session_sink
from .kernel.telemetry import (
    derive_session_id,
    metadata_extras,
    metadata_heartbeat_loop,
    report_telemetry,
    shutdown_cleanup,
)
from .kernel.work_provider import WorkProvider
from .mode_selector import ModeSelector
from . import modes as _modes
from .modes.base import DEFAULT_MODE
from .applications.issue_pr.provider import IssuePrWorkProvider
from .status_dashboard import SessionStatus, StatusDashboard
from .tracker import Intent, TrackerAdapter
from .workspace import WorkspaceManager
from .paths import AUDIT_LOG, ORCHESTRATORD_BASE

if TYPE_CHECKING:
    from .applications.issue_pr.lifecycle import IssueToPrLifecycle
    from .tracker import CommandIntent
    from orchestratord.spi.backend import AgentBackend

logger = logging.getLogger(__name__)

_CONTINUATION_RETRY_DELAY_MS = 1_000
# End reasons that mean SUCCESS — the failure-guidance block must skip
# them (they are not in the failure guidance table and would otherwise
# fall back to the generic "未知错误" fallback).
_SUCCESS_END_REASONS = frozenset({"success"})

# Compatibility lookup only.  Implementations belong to the issue→PR
# application; keeping this allow-list closed prevents the composition root
# from silently becoming a general-purpose business service locator.
_ISSUE_APPLICATION_METHODS = frozenset(
    {
        "_dependencies_satisfied",
        "_process_escalated_issues",
        "_process_review_feedback",
        "_process_pending_rebase_conflicts",
        "_process_pr_conflict_scan",
        "_resolve_intent",
        "_resolve_command_intent",
        "_post_command_acknowledgement",
        "_is_command_author_eligible",
        "_reject_unauthorized_command",
        "_check_retry_rate_limit",
        "_post_retry_rejection",
        "_prepare_intent_reset",
        "_sync_tracker_issue_state",
        "_check_rebase_rate_limit",
        "_process_rebase_intent",
        "_launch_rebase_resolution",
        "_finalize_rebase_resolution",
        "_rebase_conflict_resolved",
        "_prepare_rebase_session",
        "_prepare_intent_session",
        "_uses_review_feedback_followup",
        "_complete_read_only_chat_followup",
        "_launch_followup_with_pending_reviews",
        "_launch_review_followup",
        "_update_issue_summary",
        "_apply_review_rules",
        "_reply_to_processed_feedback",
        "_post_feedback_summary",
        "_handle_rebase_control",
        "_handle_review_followup_control",
        "_handle_review_retry_control",
        "_handle_review_approve_control",
        "_handle_retry_control",
        "_handle_followup_control",
    }
)


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


def _im_command_flags(payload: str) -> dict[str, str]:
    """Parse ``--flag value`` / ``--flag=value`` pairs from an IM command.

    Supports the flag shapes used by the IM issue-cli verbs (``--id``,
    ``--approve``, ``--feedback "..."`` etc.). A flag with no value maps to
    ``""`` so callers can distinguish presence via ``name in flags``.
    """
    try:
        tokens = shlex.split((payload or "").strip())
    except ValueError:
        tokens = (payload or "").split()
    flags: dict[str, str] = {}
    i = 0
    while i < len(tokens):
        token = tokens[i]
        if token.startswith("--") and len(token) > 2:
            name, sep, value = token.partition("=")
            if sep:
                flags[name[2:]] = value
            elif i + 1 < len(tokens) and not tokens[i + 1].startswith("--"):
                flags[name[2:]] = tokens[i + 1]
                i += 1
            else:
                flags[name[2:]] = ""
        i += 1
    return flags


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
        kernel_hooks: KernelHooks | None = None,
        # P4 C2c 组合根 seam（DESIGN §4.2/G2）：Application / WorkProvider
        # 由组合根供给。注入的实例由组合根以无宿主形态构造，此处回绑
        # ``_host``（两段式装配）；``None`` 保直接构造的自建路径（大量
        # 测试直构 Orchestrator）。
        application: IssueToPrLifecycle | None = None,
        work_provider: IssuePrWorkProvider | None = None,
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
        # 宿主装配钩子（DESIGN §4.7）：server 经 KernelHooks 注入 IM 网关
        # 装配，取代对 Orchestrator.run 的 monkey-patch。
        self._kernel_hooks = kernel_hooks
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

        # Crash telemetry: read the PREVIOUS daemon's metadata before this
        # process overwrites it. Leftover metadata with a stale pid means
        # the previous daemon exited without graceful cleanup
        # (SIGKILL/OOM/panic) — reported as a crash event at run() start.
        self._previous_unclean_exit: dict | None = None
        try:
            from .workspace_locator import read_orchestrator_metadata

            prev = read_orchestrator_metadata(workspace_root)
            if prev and prev.get("pid") not in (None, os.getpid()):
                self._previous_unclean_exit = prev
        except Exception:
            self._previous_unclean_exit = None

        # Write orchestrator metadata for CLI discovery
        self._metadata_started_at = time.time()
        from .workspace_locator import write_orchestrator_metadata

        write_orchestrator_metadata(
            workspace_root=workspace_root,
            workflow_path=self._workflow_path,
            started_at=self._metadata_started_at,
            **self._metadata_extras(),
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
        # P4 WorkProvider seam (DESIGN §4.1): the per-poll business chain
        # (fetch → intent gates → launch) lives in the issue→PR
        # application's ``IssuePrWorkProvider`` (C2a onward). A supplied
        # instance arrives hostless from the composition root (C2c
        # two-phase assembly) and is bound here.
        if work_provider is not None:
            work_provider._host = self
            self._work_provider: WorkProvider = work_provider
        else:
            self._work_provider: WorkProvider = IssuePrWorkProvider(self)
        # P4 Application seam (DESIGN §4.2): the issue→PR business
        # lifecycle (launch assembly, session decoration, result
        # interpretation, control-command registry) lives in the
        # application layer (C2b onward). Function-level import — this
        # module must not import applications at top level (import
        # timing contract); lifecycle.py does not touch the
        # orchestration_subsystem seam, so eager construction is safe.
        if application is not None:
            bind_host = getattr(application, "bind_host", None)
            if callable(bind_host):
                bind_host(self)
            else:
                application._host = self
            self._issue_app = application
        else:
            from orchestratord.applications.issue_pr.lifecycle import IssueToPrLifecycle

            self._issue_app = IssueToPrLifecycle(self)
        bind_application = getattr(self._work_provider, "bind_application", None)
        if callable(bind_application):
            bind_application(self._issue_app)
        # The Kernel owns loop cadence, task bookkeeping and poll dispatch.
        # Keep the historical attributes as aliases for compatibility with
        # CLI/tests while making the ownership boundary explicit.
        self._kernel = OrchestrationKernel(
            runtime=self,
            state=self._state,
            shutdown_event=self._shutdown_event,
            hooks=self._kernel_hooks,
        )
        self._semaphore = self._kernel.semaphore
        self._tasks = self._kernel.tasks
        self._issue_tasks = self._kernel.run_tasks
        # Do NOT keep a single :class:`ProgressReporter` here.
        # Per-session progress is fanned out via
        # :meth:`_build_session_sink` (a fresh
        # :class:`CompositeProgressSink` rooted in a private
        # :class:`ToolContextProgressSink`) so concurrent issues can no
        # longer share ``_current_task_id`` / ``_phase_count`` state.
        # The shared ``_progress_context`` stays because every
        # per-session :class:`ToolContextProgressSink` writes into the
        # same ``ToolContext.tasks[id].metadata.progress_stages`` dict.

    def __getattr__(self, name: str) -> Any:
        """Resolve the finite legacy business surface through the app.

        This keeps old integrations that call ``orch._resolve_intent()``
        source-compatible while ensuring the named implementation is owned
        by ``applications.issue_pr``. Unknown names fail normally.
        """
        if name not in _ISSUE_APPLICATION_METHODS:
            raise AttributeError(name)
        try:
            app = object.__getattribute__(self, "_issue_app")
        except AttributeError:
            # Lightweight __new__ test/CLI adapters still use the same
            # application boundary; bind it lazily without reviving a
            # composition-root implementation fallback.
            from .applications.issue_pr.lifecycle import IssueToPrLifecycle

            app = IssueToPrLifecycle(self)
            object.__setattr__(self, "_issue_app", app)
        for target in (
            getattr(app, "_interpretation", None),
            getattr(app, "_commands", None),
        ):
            if target is not None and hasattr(target, name):
                return getattr(target, name)
        raise AttributeError(name)

    @property
    def _rebase_for_pr(self) -> Any:
        """Compatibility seam for application-owned rebase execution."""
        return rebase_for_pr

    def _build_session_sink(self, task_id: str) -> Any:
        """Build a fresh :class:`CompositeProgressSink` for one session.

        基础组装与 Asciicast 挂载已迁 kernel/sink_router.py（DESIGN §5）；
        IM/channel sink 挂载段为宿主钩子接缝（KernelHooks
        ``on_session_sink_build``，DESIGN §4.7，P4 步骤 5 正式化）。
        """
        composite = build_base_session_sink(
            task_id=task_id,
            workflow=self.workflow,
            progress_context=getattr(self, "_progress_context", None),
        )
        # P3: attach the IM event emitter when a deliver callback is wired.
        if getattr(self, "im_event_deliver", None) is not None:
            from .sinks.channel import ChannelProgressSink
            from .events import OrchestratorEvent, OrchestratorEventEmitter
            from .applications.issue_pr.payloads import issue_payload_for_task_id

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
                    payload=issue_payload_for_task_id(
                        getattr(self, "tracker", None),
                        getattr(self, "_registry", None),
                        task_id,
                    ),
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
        attach_asciicast_sink(
            composite,
            capture=getattr(self, "asciicast_capture", None),
            task_id=task_id,
            phases=self.workflow.agent.phases,
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

    def _register_collaboration_modes(
        self, workflow: WorkflowConfig, agent_runner: BackendRunner
    ) -> None:
        """机制段已迁 kernel/mode_dispatcher.py（DESIGN §5）；保留薄转发。"""
        register_collaboration_modes(workflow, agent_runner)

    def _build_mode_selector(self, workflow: WorkflowConfig) -> ModeSelector:
        """机制段已迁 kernel/mode_dispatcher.py（DESIGN §5）；保留薄转发。"""
        return build_mode_selector(workflow)

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
        """Compatibility entry point; the Kernel owns the daemon loop."""
        await self._kernel.run()

    async def _kernel_start(self) -> tuple[float, str]:
        """Prepare host resources for :class:`OrchestrationKernel`."""
        # 宿主钩子（DESIGN §4.7）：在进入轮询/sink 组装前调用 on_kernel_start，
        # 宿主在此完成 IM 网关等会话级装配（取代旧 monkey-patch 的注入时点）。
        if self._kernel_hooks is not None:
            on_start = getattr(self._kernel_hooks, "on_kernel_start", None)
            if on_start is not None:
                await on_start(self)
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

        # Crash telemetry: the previous daemon for this workspace died
        # without reaching shutdown_cleanup (see __init__ detection).
        if self._previous_unclean_exit:
            try:
                from orchestratord.telemetry import record_crash

                prev = self._previous_unclean_exit
                record_crash(
                    session_id=orch_session_id,
                    kind="daemon_unclean_shutdown",
                    previous_pid=prev.get("pid"),
                    previous_started_at=prev.get("started_at"),
                )
            except Exception:
                pass

        # Clean up terminal workspaces on startup
        await self.workspace.run_terminal_workspace_cleanup()
        await self._recover_stale_running_records()
        # Rebuild the retry queue from persisted retry plans so a
        # scheduled retry survives daemon restarts.
        self._recover_pending_retries()
        await self._recover_persistent_states()

        return orch_start, orch_session_id

    async def _kernel_record_error(self, exc: BaseException) -> None:
        try:
            from orchestratord.telemetry import record_error

            record_error(session_id=self._derive_orchestrator_session_id(), exc=exc)
        except Exception:
            pass

    async def _kernel_apply_outcome(self, item: Any, outcome: Any) -> None:
        """Runtime bridge for outcomes already interpreted by IssueToPr."""
        # IssueToPr's interpreter intentionally keeps registry/tracker side
        # effects together for behavioral parity.  Kernel still records the
        # decision and owns any future generic scheduling extension.
        self._last_kernel_outcome = (item.dedup_key, outcome)

    async def _kernel_finish(self, started: tuple[float, str], exit_status: int) -> None:
        try:
            from orchestratord.telemetry import record_command_run, record_session_end

            orch_start, orch_session_id = started
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
        """机制段已迁 kernel/telemetry.py（DESIGN §5）；保留薄转发。"""
        return derive_session_id(self._workspace_root)

    def _report_telemetry(self) -> None:
        """机制段已迁 kernel/telemetry.py（DESIGN §5）；保留薄转发。"""
        report_telemetry(self.workflow)

    async def _recover_stale_running_records(self) -> None:
        reason = "Recovered stale running issue on orchestrator startup"
        stale_records = self._registry.running_records()
        for record in stale_records:
            self._registry.mark_failed_with_reason(record.issue_id, reason)
            # 非破坏性恢复：不走 _sync_tracker_issue_state("failed") 关单，
            # tracker issue 保持 open 以便重试可被拾取；把重试计划持久化到
            # registry 记录（retry_count / next_retry_at），由随后的
            # _recover_pending_retries 在同一启动序列中重建 retry queue
            # （与普通失败路径 _schedule_retry 的持久化字段一致）。
            recovered = self._registry.get(record.issue_id)
            if recovered is not None:
                delay_ms = compute_retry_delay(
                    1,
                    FAILURE_RETRY_BASE_MS,
                    self.workflow.agent.max_retry_backoff_ms,
                )
                recovered.retry_count = 1
                recovered.next_retry_at = time.time() + delay_ms / 1000.0
                recovered.touch()
                self._registry._save()
            logger.warning(
                "Recovered stale running issue_id=%s on orchestrator startup "
                "(retry scheduled)",
                record.issue_id,
            )

    def _recover_pending_retries(self) -> None:
        """Rebuild the retry queue from persisted retry plans.

        ``_schedule_retry`` persists ``next_retry_at`` on the registry
        record so a waiting retry survives daemon restarts.  Nothing
        read that field back, so after a restart the retry silently
        evaporated — the record kept ``next_retry_at`` (non-terminal
        planning) but the in-memory ``retry_queue`` was rebuilt empty
        and the issue hung forever.

        This startup path scans the registry for records with a pending
        retry plan and reconstructs their ``RetryItem`` entries.  The
        items are then consumed by the normal ``_process_retry_queue``
        dispatch, which applies the existing guards unchanged
        (concurrency slots, tracker active-state check, requeue
        ceiling).  No launch happens here — a full queue at startup
        still respects ``max_concurrent_agents``.
        """
        recovered = self._registry.pending_retry_records()
        if not recovered:
            return
        now = time.time()
        for record in recovered:
            remaining = max(0.0, record.next_retry_at - now)
            attempt = record.retry_count or 1
            retry = RetryItem(
                dedup_key=record.issue_id,
                attempt=attempt,
                # Remaining wait until the persisted due time; overdue
                # plans (``next_retry_at`` in the past, e.g. the daemon
                # was down while the retry came due) become due
                # immediately (delay 0).
                delay_seconds=remaining,
                identifier=record.issue_identifier,
                error="recovered persisted retry plan on orchestrator startup",
            )
            self._state.retry_queue.append(retry)
            # Restore the attempt counter so the retry-limit guard in
            # ``_schedule_retry`` (``max_retry_attempts``) is not
            # bypassed by a restart resetting it to zero.
            self._state.retry_attempts[record.issue_id] = attempt
            logger.info(
                "Recovered pending retry issue_id=%s attempt=%s "
                "next_retry_at=%s remaining=%.0fs",
                record.issue_id,
                attempt,
                record.next_retry_at,
                remaining,
            )

    async def _recover_persistent_states(self) -> None:
        """Restore in-memory state sets from persistent registry records.

        On daemon restart the in-memory ``_state`` sets (``completed``,
        ``pending_review``) are empty.  Without this recovery, an issue
        whose registry record is ``PENDING_REVIEW`` would be treated as a
        fresh candidate and re-launched — producing duplicate work on the
        already-existing PR branch.

        ``COMPLETED`` records are re-hydrated too: the tracker usually
        closes completed issues, but restoring the set keeps the
        candidate-issue poll loop consistent without consulting the
        tracker API.
        """
        from .issue_registry.models import IssueStatus

        for record in self._registry.records_by_status(IssueStatus.PENDING_REVIEW):
            self._state.pending_review.add(record.issue_id)
            logger.info(
                "Recovered pending_review state for issue_id=%s on startup",
                record.issue_id,
            )

        for record in self._registry.records_by_status(IssueStatus.COMPLETED):
            self._state.completed.add(record.issue_id)
            logger.info(
                "Recovered completed state for issue_id=%s on startup",
                record.issue_id,
            )

    async def _metadata_heartbeat_loop(self) -> None:
        """机制段已迁 kernel/telemetry.py（DESIGN §5）；保留薄转发。"""
        await metadata_heartbeat_loop(
            workspace_root=self._workspace_root,
            workflow_path=self._workflow_path,
            started_at=self._metadata_started_at,
            shutdown_event=self._shutdown_event,
            extras_provider=self._metadata_extras,
        )

    def _metadata_extras(self) -> dict:
        """机制段已迁 kernel/telemetry.py（DESIGN §5）；保留薄转发。"""
        return metadata_extras(self.workflow, self.agent_runner, self._backend)

    async def shutdown(self) -> None:
        """Signal graceful shutdown and clean up metadata."""
        self._shutdown_event.set()
        shutdown_cleanup(self._workspace_root)

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
        """Compatibility seam for callers that drive one Kernel tick."""
        kernel = getattr(self, "_kernel", None)
        if kernel is None:
            # A few embedders construct a lightweight Orchestrator test
            # double without running __init__.  Preserve that supported seam
            # while still routing the actual cycle through Kernel.
            kernel = OrchestrationKernel(
                runtime=self,
                state=self._state,
                shutdown_event=getattr(self, "_shutdown_event", asyncio.Event()),
            )
            self._kernel = kernel
        else:
            # Keep compatibility with tests/embedders that replace the state
            # object after construction.
            kernel.state = self._state
        if hasattr(self, "_tasks"):
            kernel.tasks = self._tasks
        if hasattr(self, "_issue_tasks"):
            kernel.run_tasks = self._issue_tasks
        await kernel.dispatch_once()





    # ------------------------------------------------------------------
    # Role check + rate-limit guard
    # ------------------------------------------------------------------





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


    # ------------------------------------------------------------------
    # PR Conflict Auto-Resolution
    # ------------------------------------------------------------------






    async def _launch_issue(self, issue: Issue) -> None:
        """Launch one issue: 业务装配在应用侧（3-seam 保序委托，C2b），
        本壳只做机制段——AgentSession/pause 回调、viz journal、running
        map、task 创建。"""
        # 业务装配段已迁应用侧（C2b，DESIGN §4.2 prepare seam）：依赖
        # 复检、intent reset、branch 推导、workspace 创建、registry
        # 记账、tracker 刷新守卫。None = gated / skip（既有早退路径，
        # 副作用 claimed.discard / completed.add 已随迁）。
        app = self._issue_app
        prepared = await app.prepare_launch(issue)
        if prepared is None:
            return
        business = prepared.business
        workspace = business["workspace"]

        session = AgentSession(
            subject=issue,
            workspace=workspace,
            conversation_id=business["conversation_id"],
            parent_run_id=business["parent_run_id"],
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
        # 会话装饰迁应用侧（C2b）：clarification 注入、retry 上下文、
        # 协作模式选择 + registry 记账；mode_decision 经返回值转交，
        # 供下方 viz journal 的 phase 事件使用。
        mode_decision = await app.decorate_session(session, prepared)
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
        # viz 后业务门迁应用侧（C2b，3-seam 第三缝）：review-feedback
        # followup 早退、intent session 准备、previous_run_ids /
        # previous_verification_error 注入。False = 已处理 / 跳过
        # （session 弃置，不进入机制尾）。
        if not await app.post_viz_gate(session):
            return
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
            backend=getattr(session, "_snapshot_backend", None) or None,
            model=getattr(session, "_snapshot_model", None) or None,
            session_end_reason=getattr(session, "session_end_reason", None),
            session_end_summary=getattr(session, "session_end_summary", None),
            pause_reason=getattr(session, "pause_reason", None),
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
            # run_for_issue 已删除（机制域不得内嵌业务转换，DESIGN §3.2）：
            # Issue→AgentTask 的业务映射留在业务侧完成后走通用入口。
            from .issue_registry.task_mapping import issue_to_agent_task

            result = await workflow_orch.run_for_task(
                issue_to_agent_task(
                    session.issue,
                    workspace_path=str(session.workspace.path),
                )
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

        # 工作流引擎在 per-stage session 上设置 _snapshot_backend，
        # 外层 session 不会被设置，run report 的 Backend 字段会显示 n/a。
        # 从 agent_runner 回填，确保 report 能正确展示后端名称。
        _backend = getattr(self.agent_runner, "backend", None)
        if _backend is not None:
            session._snapshot_backend = getattr(_backend, "name", None) or ""
            from .cost.estimator import resolve_model_alias

            _agent_config = getattr(self.agent_runner, "agent_config", None)
            session._snapshot_model = resolve_model_alias(
                getattr(_agent_config, "model", None) or "",
                getattr(_agent_config, "model_aliases", None) or {},
            )
            session._snapshot_provider = (
                getattr(self.agent_runner.agent_config, "provider", None)
                or getattr(_backend, "name", "")
            )

        self._update_run_diagnostics(session)

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
        """Compatibility entry point; application owns issue execution."""
        await self._issue_app.execute_session(session)






    async def _schedule_retry(
        self,
        session: AgentSession,
        *,
        delay_base_ms: int | None = None,
    ) -> bool:
        """Schedule a retry for a failed session.

        ``delay_base_ms`` overrides the base delay for the exponential backoff
        curve. When ``None`` the default ``FAILURE_RETRY_BASE_MS`` is used
        (10s). The orchestrator passes ``workflow.agent.max_turns_retry_delay_ms``
        for ``max_turns_exceeded`` sessions so the longer wait default kicks in
        without forcing all retries to share it.

        Returns ``True`` when a retry was actually queued. Callers gate
        their tracker state sync on this: a pending retry must keep the
        issue open on the tracker (closing it here — and losing the
        reopen, which GitCode cannot perform — silently drops the
        persisted retry plan when the poller sees the closed state).
        Terminal outcomes (retry exhausted / non-retryable end reason)
        return ``False`` so the caller still closes the issue.
        """
        issue_id = session.issue.id or ""

        # Operator-initiated stops must never be defeated by the
        # auto-retry loop (stop → retry → stop burning API calls until
        # max attempts). These end reasons are terminal; the operator
        # can run the issue again explicitly via ``issue retry``.
        end_reason = getattr(session, "session_end_reason", None)
        if end_reason in NON_RETRYABLE_END_REASONS:
            logger.warning(
                "Not auto-retrying issue_id=%s — end reason '%s' is "
                "operator-initiated; use 'issue retry' to run again",
                issue_id,
                end_reason,
            )
            self._state.claimed.discard(issue_id)
            return False

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
            return False

        # Exponential backoff capped at max_retry_backoff_ms
        base_ms = delay_base_ms if delay_base_ms is not None else FAILURE_RETRY_BASE_MS
        max_ms = self.workflow.agent.max_retry_backoff_ms
        delay_ms = compute_retry_delay(attempt, base_ms, max_ms)

        retry = RetryItem(
            dedup_key=issue_id,
            attempt=attempt,
            delay_seconds=delay_ms / 1000.0,
            identifier=session.issue.identifier or "",
            error=f"agent failed: {session.status}",
        )
        self._state.retry_queue.append(retry)
        # Persist the retry plan on the registry record so a
        # daemon restart cannot silently drop a waiting retry and
        # operators can see why nothing is running.
        self._registry.persist_retry_plan(
            issue_id,
            retry_count=attempt,
            next_retry_at=retry.scheduled_at + retry.delay_seconds,
        )
        logger.info(
            "Scheduled retry issue_id=%s attempt=%s delay=%sms",
            issue_id,
            attempt,
            delay_ms,
        )
        try:
            from orchestratord.telemetry import record_retry_scheduled

            record_retry_scheduled(
                session_id=getattr(session, "session_id", None)
                or getattr(session, "run_id", None)
                or "",
                run_id=getattr(session, "run_id", None) or "",
                issue_id=issue_id,
                attempt=attempt,
                delay_ms=delay_ms,
                reason=f"agent failed: {session.status}",
            )
        except Exception:
            logger.debug("retry telemetry failed", exc_info=True)
        self._emit_im_event(
            issue_id,
            "intent.retry",
            EventLevel.INFO,
            f"retry scheduled in {delay_ms}ms",
            {"attempt": attempt, "delay_ms": delay_ms},
        )
        return True

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


    def _retry_requeue_limit(self) -> int:
        """Ceiling for tracker-miss requeues.

        A retry that the tracker permanently fails to report must not
        loop forever; the cap mirrors ``max_retry_attempts``.
        """
        return max(1, int(getattr(self.workflow.agent, "max_retry_attempts", 5) or 5))

    @staticmethod
    def _retry_dedup_key(retry: Any) -> str:
        """Read the generic key while accepting legacy retry test doubles."""
        return str(getattr(retry, "dedup_key", None) or getattr(retry, "issue_id", ""))

    def _requeue_retry(self, retry: Any, now: float) -> bool:
        """Re-queue a retry with a doubled, capped delay.

        Returns ``False`` when the requeue ceiling is exhausted — the
        item is dropped (with a warning) and the persisted plan cleared.
        """
        requeued = requeue_retry_item(
            retry,
            now,
            requeue_limit=self._retry_requeue_limit(),
            max_backoff_ms=self.workflow.agent.max_retry_backoff_ms,
        )
        if not requeued:
            logger.warning(
                "Retry issue %s exceeded the requeue ceiling (%d) — "
                "dropping the persisted retry plan; use 'issue retry' "
                "to run again",
                self._retry_dedup_key(retry),
                self._retry_requeue_limit(),
            )
            self._clear_retry_plan(self._retry_dedup_key(retry))
            return False
        self._state.retry_queue.append(retry)
        return True

    def _clear_retry_plan(self, issue_id: str) -> None:
        """Clear ``next_retry_at`` on the registry record (best-effort)."""
        self._registry.clear_retry_plan(issue_id)

    async def _process_retry_queue(self) -> None:
        """Process retry queue with exponential backoff.

        Retries are processed before new candidate issues so that
        previously-failed work gets priority.
        """
        import time

        now = time.time()
        ready, not_ready = split_ready_retries(self._state.retry_queue, now)

        # The queue is rewritten before processing so retention no
        # longer depends on ``remaining`` aliasing the live list;
        # deferred items re-appended below land on the fresh list.
        self._state.retry_queue = not_ready

        for retry in ready:
            # Skip if already running or completed
            retry_key = self._retry_dedup_key(retry)
            if retry_key in self._state.running or retry_key in self._state.completed:
                logger.debug("Retry skipped dedup_key=%s already running/completed", retry_key)
                continue

            # Check concurrency slot
            if len(self._state.running) >= self._state.max_concurrent_agents:
                logger.debug("Retry deferred dedup_key=%s no concurrency slots", retry_key)
                self._state.retry_queue.append(retry)
                continue

            # Re-fetch issue state from tracker
            try:
                issues = await self.tracker.fetch_issue_states_by_ids([retry_key])
                issue = issues.get(retry_key)
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
                            retry_key,
                            retry.requeue_count,
                            self._retry_requeue_limit(),
                        )
                    continue
            except Exception as exc:
                logger.error("Failed to fetch retry dedup_key %s: %s", retry_key, exc)
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
                    retry_key,
                    issue.state,
                )
                self._clear_retry_plan(retry_key)
                continue

            self._state.claimed.add(retry_key)
            # 先 launch 再清 plan：`_launch_issue` 可能因守卫 /
            # workspace / prepare_launch / post_viz_gate 早退，此时
            # 若已清 plan，重试会静默丢失且不标失败。launch 成功以
            # issue 进入 running map 为准；早退则把 retry 放回队列，
            # 保留下一次机会（不会静默蒸发）。
            await self._launch_issue(issue)
            if issue.id in self._state.running:
                # The plan is being executed — clear next_retry_at so
                # the registry reflects reality.
                self._clear_retry_plan(retry_key)
                logger.info(
                    "Retry launched issue_id=%s attempt=%s",
                    retry_key,
                    retry.attempt,
                )
            else:
                # Launch was gated/skipped (guard / workspace /
                # _prepare_intent_reset early exit). Re-queue so the
                # retry is not silently lost; the same guard will skip
                # it again if the blocker persists.
                logger.warning(
                    "Retry launch gated for issue_id=%s — restoring retry plan",
                    retry_key,
                )
                self._state.retry_queue.append(retry)

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
            for control_file in sorted(
                control_dir.iterdir(), key=lambda path: path.name
            ):
                if not control_file.name.endswith(".control"):
                    continue
                parts = control_file.read_text(encoding="utf-8").strip().split("\n")
                if not parts:
                    continue
                cmd = parts[0].strip()
                issue_id = parts[1].strip() if len(parts) > 1 else ""
                extra = "\n".join(parts[2:]).strip() if len(parts) > 2 else ""

                remove_control_file = True
                try:
                    # 业务命令注册表（C2b，DESIGN §4.2 control_commands）：
                    # review_followup / rebase / review_approve /
                    # review_retry / retry / followup 由应用声明，handler
                    # 返回值决定控制文件是否删除（followup 可保留）；
                    # gateway_* 与 pause/resume/stop/takeover 等机制命令
                    # 留宿主。
                    business_handler = self._issue_app.control_commands().get(cmd)
                    if business_handler is not None:
                        remove_control_file = bool(
                            await business_handler(issue_id, extra)
                        )
                    elif cmd in {"gateway_connect", "gateway_disconnect"}:
                        await self._handle_gateway_control(cmd, extra)
                    else:
                        self._apply_control_command(cmd, issue_id, extra)
                finally:
                    if remove_control_file:
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

    def _spawn_im_control(self, coro) -> None:
        """Run an async IM control handler off the gateway read loop.

        The IM handlers are invoked from the IPC read loop's sequential
        ``on_deliver`` dispatch, so async control paths must not be awaited
        inline. The task is tracked in ``self._tasks`` (cancelled by the
        normal shutdown path) and failures are logged.
        """
        tasks = getattr(self, "_tasks", None)
        if tasks is None:
            tasks = set()
            self._tasks = tasks
        task = asyncio.create_task(coro)
        tasks.add(task)
        task.add_done_callback(self._on_im_control_task_done)

    def _on_im_control_task_done(self, task: asyncio.Task) -> None:
        tasks = getattr(self, "_tasks", None)
        if tasks is not None:
            tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            logger.warning("IM gateway control task failed", exc_info=task.exception())

    def _im_followup_hints_path(self, issue_id: str) -> Path:
        """Resolve where an IM follow-up's hints text should be recorded.

        The follow-up prompt is read from ``.operator_hints.md`` in the
        issue's workspace by ``prompt_builder`` at launch time, so prefer
        the registry record's workspace; fall back to the workspace root.
        """
        record = None
        registry = getattr(self, "_registry", None)
        if issue_id and registry is not None:
            try:
                record = registry.get(issue_id)
            except Exception:  # noqa: BLE001
                record = None
        workspace_path = getattr(record, "workspace_path", None) if record else None
        if workspace_path:
            return Path(workspace_path) / ".operator_hints.md"
        return self._workspace_root / ".operator_hints.md"

    def _apply_im_followup(self, issue_id: str, text: str) -> None:
        """IM ``followUp`` handler — record hints + requeue via follow-up control.

        SPEC im-gateway Phase 4: a follow-up must reach the real control
        path, not just be logged. The text lands in ``.operator_hints.md``
        (the follow-up prompt source), and the existing
        ``_handle_followup_control`` re-queues the issue for a FOLLOWUP
        re-run. Without an issue_id the hints file is the durable fallback.
        """
        try:
            hints_path = self._im_followup_hints_path(issue_id)
            hints_path.parent.mkdir(parents=True, exist_ok=True)
            with hints_path.open("a", encoding="utf-8") as f:
                f.write(f"\n{text}\n")
        except Exception:
            logger.warning("IM followup: failed to write operator hints", exc_info=True)
        if not issue_id:
            logger.info(
                "IM followup without issue_id: text recorded in .operator_hints.md; "
                "no issue re-queue"
            )
            return
        self._spawn_im_control(self._handle_followup_control(issue_id, text))
        logger.info("IM followup queued: issue=%s text_len=%d", issue_id, len(text))

    def _apply_im_issue_cli(self, verb: str, issue_id: str, payload: str) -> None:
        """IM issue-cli handler — map verbs onto existing control handlers.

        Mirrors the control-file commands consumed by
        ``_process_control_commands`` so IM slash commands hit the same
        paths as the CLI: review approvals/rejections, PR feedback
        follow-ups, retries, rebases, and clarification answers.
        """
        verb = (verb or "").strip().lower()
        if not verb:
            return
        flags = _im_command_flags(payload or "")
        issue_id = issue_id or flags.get("id") or ""

        if verb == "retry":
            self._apply_control_command("retry", issue_id, flags.get("reason", ""))
            return
        if verb == "rebase":
            extra = f"force={'1' if 'force' in flags else '0'}\n{flags.get('reason', '')}"
            self._spawn_im_control(self._handle_rebase_control(issue_id, extra))
            return
        if verb == "review":
            if "approve" in flags:
                self._spawn_im_control(
                    self._handle_review_approve_control(issue_id, flags.get("comment", ""))
                )
                return
            if "reject" in flags:
                feedback = flags.get("feedback", "")
                if not feedback:
                    logger.warning("IM review reject without --feedback: issue=%s", issue_id)
                    return
                self._spawn_im_control(self._handle_review_retry_control(issue_id, feedback))
                return
            logger.warning("IM review without --approve/--reject: issue=%s", issue_id)
            return
        if verb == "feedback":
            if "dismiss" in flags:
                self._im_dismiss_feedback(issue_id, flags.get("feedback-id", ""))
                return
            # Default mirrors the CLI: approve pending feedback by queueing
            # a review follow-up that addresses it.
            self._spawn_im_control(
                self._handle_review_followup_control(issue_id, flags.get("feedback-id", ""))
            )
            return
        if verb == "clarify":
            answer = flags.get("answer", "")
            if not answer:
                logger.info("IM clarify without --answer: issue=%s (ignored)", issue_id)
                return
            resolved = self._clarification_queue.resolve(
                issue_id, answer, source="clarification_queue"
            )
            if resolved is None:
                logger.info("IM clarify: no pending clarification for issue=%s", issue_id)
            else:
                logger.info("IM clarify answered: issue=%s", issue_id)
            return
        logger.info("IM issue_cli verb %s has no direct control path; ignored", verb)

    def _im_dismiss_feedback(self, issue_id: str, feedback_id: str) -> None:
        """IM ``/feedback --dismiss`` — mark pending feedback items processed."""
        record = self._registry.get(issue_id) if issue_id else None
        target_ids = (
            [fid.strip() for fid in feedback_id.split(",") if fid.strip()]
            if feedback_id
            else list(getattr(record, "pending_feedback_ids", None) or [])
        )
        if record is None or not target_ids:
            logger.info("IM feedback dismiss: nothing pending for issue=%s", issue_id)
            return
        self._registry.mark_feedback_processed(issue_id, target_ids)
        logger.info("IM feedback dismissed: issue=%s items=%d", issue_id, len(target_ids))

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
            # followUp → real follow-up control path (hints + requeue).
            queue_pending_message=self._apply_im_followup,
            control_verb=_control_verb,
            issue_inject=_issue_inject,
            # contextOnly → hints only, never triggers a new run.
            operator_hints=_issue_inject,
            agent_intent=_control_verb,
            # clarify/review/feedback (and defensive retry/rebase) → the
            # daemon's existing issue control handlers.
            issue_cli=self._apply_im_issue_cli,
            # interrupt → stop (cancels the running issue task).
            bridge_interrupt=lambda issue_id, payload: _control_verb("stop", issue_id),
        )
        session_id = f"orchestrator-{os.getpid()}-{int(time.time() * 1000)}"
        ipc = GatewayIpcClient(sock, instance_id=session_id)
        from orchestratord.commands.service import OrchestratorCommandService

        command_service = OrchestratorCommandService(
            workspace_root=self._workspace_root,
            workflow_path=getattr(self, "_workflow_path", None),
            runtime_supplier=lambda: self,
        )
        wrapper = OrchestratorGatewayClient(
            handlers, ipc_client=ipc, origin=origin, command_router=None, control_bridge=None,
            command_service=command_service
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
        from .sinks.channel import deliver_event_via_client

        loop = asyncio.get_running_loop()

        def _sync_deliver(event, text):
            # Forward the event metadata envelope (issue_id/event_type/
            # level/markdown) through the client when it supports it.
            deliver_event_via_client(wrapper, event, text, loop=loop)

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
            self._state.retry_queue = [
                retry for retry in retry_queue if self._retry_dedup_key(retry) != issue_id
            ]
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





    def _cancel_pending_retry(self, issue_id: str) -> bool:
        """Cancel any pending auto-retry for ``issue_id``.

        Removes the issue from the in-memory retry queue and clears the
        persisted retry plan on the registry record.  Returns ``True``
        when a pending retry actually existed and was cancelled.

        Used by stop/takeover control commands so an operator action is
        never defeated by an already-scheduled auto-retry.
        """
        in_queue = False
        retry_queue = getattr(self._state, "retry_queue", None)
        if retry_queue is not None:
            in_queue = any(self._retry_dedup_key(r) == issue_id for r in retry_queue)
            if in_queue:
                self._state.retry_queue = [
                    r for r in retry_queue if self._retry_dedup_key(r) != issue_id
                ]
        if hasattr(self, "_registry") and hasattr(self._registry, "get"):
            record = self._registry.get(issue_id)
            had_plan = record is not None and record.next_retry_at is not None
            if had_plan:
                self._registry.clear_retry_plan(issue_id)
            return in_queue or had_plan
        return in_queue

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

        # Terminal operator commands (stop / takeover) must defeat any
        # pending auto-retry.  An issue waiting in ``retry_queue`` is
        # not in ``running`` — the running-session branch below would
        # silently drop the command while the persisted plan still fired
        # at ``next_retry_at``.  The operator's action must cancel the
        # queued retry, persist the end reason, and close the tracker
        # issue so the retry never fires.
        if cmd in ("stop", "takeover") and issue_id:
            end_reason = "operator_stop" if cmd == "stop" else "operator_takeover"
            cancelled = self._cancel_pending_retry(issue_id)
            if cancelled:
                logger.info(
                    "Control %s cancelled pending retry for issue %s",
                    cmd,
                    issue_id,
                )
                self._registry.update_report(
                    issue_id,
                    session_end_reason=end_reason,
                    session_end_summary=f"operator requested {cmd} while issue was waiting for retry",
                )
                self._state.claimed.discard(issue_id)
                retry_attempts = getattr(self._state, "retry_attempts", None)
                if retry_attempts is not None:
                    retry_attempts.pop(issue_id, None)
                self._emit_im_event(
                    issue_id,
                    f"control.{cmd}",
                    EventLevel.WARN,
                    f"{cmd} requested (pending retry cancelled)",
                )
                try:
                    loop = asyncio.get_running_loop()
                except RuntimeError:
                    loop = None
                if loop is not None:
                    loop.create_task(
                        self._sync_tracker_issue_state(issue_id, "failed")
                    )
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
            # Terminal operator action: record the end reason so the
            # auto-retry loop (which keys off NON_RETRYABLE_END_REASONS)
            # leaves the issue alone, cancel the running task, and drop
            # any scheduled retry (defensive — the item should already
            # have been removed by the retry-queue check above, but
            # there is a small window between launch and task start).
            logger.info("Stop requested for issue %s", issue_id)
            session.status = "failed"
            session.session_end_reason = "operator_stop"
            session.session_end_summary = "operator requested stop"
            session.pause_resume_event.set()  # Unblock if paused
            self._cancel_pending_retry(issue_id)
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
            # Same terminal semantics as stop — record the end reason,
            # cancel the running task, and drop any scheduled retry.
            # Without the end_reason the runner completes normally, the
            # status is overwritten, and the issue is auto-retried.
            logger.info("Takeover requested for issue %s", issue_id)
            session.status = "failed"
            session.session_end_reason = "operator_takeover"
            session.session_end_summary = "operator requested takeover"
            session.pause_resume_event.set()  # Unblock if paused
            self._cancel_pending_retry(issue_id)
            self._emit_im_event(issue_id, "control.takeover", EventLevel.WARN, "takeover requested")
            # Root-cause fix: cancel the asyncio task so the
            # CancelledError handler in _run_issue fires immediately
            # instead of leaving the agent running until the next
            # session end check.
            task = self._issue_tasks.get(issue_id)
            if task is not None and not task.done():
                task.cancel()
                logger.info("Cancelled task for issue %s", issue_id)

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


# Class-level compatibility for callers that used the former static helper.
# The implementation is kept as an assignment so the business operation's
# named definition remains in the application boundary.
Orchestrator._uses_review_feedback_followup = staticmethod(
    lambda record: bool(
        record is not None
        and record.intent is Intent.FOLLOWUP
        and record.intent_source != "chat"
    )
)
