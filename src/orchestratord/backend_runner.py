"""BackendRunner — SPI AgentBackend consumer for the orchestrator.

Provides the same ``run()`` interface as ``AgentRunner`` but consumes
the ``AgentBackend`` Protocol.  It is the backend-neutral production
entry point for every registered backend.

Phase B design:
- Provides the backend-neutral execution implementation used by the
  orchestrator and the compatibility ``AgentRunner`` wrapper.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any, Callable

from orchestratord.spi.backend import AgentBackend, SessionSpec
from orchestratord.spi.capabilities import BackendCapabilities
from orchestratord.spi.events import EventEnvelope, EventKind
from orchestratord.spi.session import ResumeStatus
from orchestratord.events.agent_events import SessionComplete, TurnComplete

from .session_state import AgentSession
from .runner_utils import _broadcast_to_socket, _drain_control_commands
from .control_socket import ControlSocket
from .agent_task import AgentTask, AgentTaskResult, ProgressEvent, ProgressEventKind
from .approval_policy import (
    ApprovalPolicy,
    ToolCallEvent,
    get_approval_policy,
)
from .config.schema import AgentConfig, SandboxConfig, WorkflowConfig, WorkspaceConfig
from .debug_log import append_debug_event
from .issue import Issue
from .prompt_builder import PromptBuilder, resolve_python_executable
from .tool_event_log import ToolEventLog

logger = logging.getLogger(__name__)

# Reuse the same noop-detection threshold as AgentRunner.
_NOOP_DETECTION_MAX_TURNS = 5


class _TaskProgressBridge:
    """Adapt legacy synchronous progress-sink calls to ``ProgressEvent``.

    The SPI event loop predates ``AgentTaskRunner`` and deliberately calls
    sink methods synchronously.  This adapter keeps that loop unchanged while
    delivering the new callback contract in event order.  Callback failures
    are isolated from agent execution, just like the legacy progress sinks.
    """

    def __init__(self, task_id: str, callback: Any | None) -> None:
        self._task_id = task_id
        self._callback = callback
        self._pending: list[asyncio.Task[Any]] = []

    def _emit(self, kind: ProgressEventKind, **kwargs: Any) -> None:
        if self._callback is None:
            return
        event = ProgressEvent(kind=kind, task_id=self._task_id, **kwargs)
        try:
            value = self._callback(event)
            if inspect.isawaitable(value):
                self._pending.append(asyncio.create_task(value))
        except Exception:
            logger.debug("progress_callback failed", exc_info=True)

    def on_text(self, text: str) -> None:
        self._emit(ProgressEventKind.TEXT, text=text)

    def on_text_delta(self, text: str) -> None:
        self._emit(ProgressEventKind.TEXT_DELTA, text=text)

    def on_tool_call(self, tool_name: str, call_id: str) -> None:
        self._emit(ProgressEventKind.TOOL_CALL, tool_name=tool_name, call_id=call_id)

    def on_tool_result(self, call_id: str) -> None:
        self._emit(ProgressEventKind.TOOL_RESULT, call_id=call_id)

    def on_turn_complete(self, event: TurnComplete, session: AgentSession) -> None:
        self._emit(
            ProgressEventKind.TURN_COMPLETE,
            turn_number=getattr(event, "turn", session.turn_count),
            tool_count=session.tool_count,
        )

    def on_session_complete(self, event: SessionComplete, session: AgentSession) -> None:
        self._emit(
            ProgressEventKind.SESSION_COMPLETE,
            turn_number=session.turn_count,
            tool_count=session.tool_count,
            message=getattr(event, "reason", ""),
        )

    def on_error(self, message: str) -> None:
        self._emit(ProgressEventKind.ERROR, message=message)

    async def flush(self) -> None:
        if not self._pending:
            return
        outcomes = await asyncio.gather(*self._pending, return_exceptions=True)
        for outcome in outcomes:
            if isinstance(outcome, Exception):
                logger.debug("progress_callback failed", exc_info=outcome)


class BackendRunner:
    """Execute an issue via an AgentBackend (SPI Protocol).

    ``BackendRunner`` accepts any ``AgentBackend`` implementation and
    drives it through the SPI.

    The ``run()`` signature is intentionally identical to
    ``AgentRunner.run()`` so that the ``Orchestrator`` can swap
    between the two without changing its own call sites.
    """

    def __init__(
        self,
        backend: AgentBackend,
        agent_config: AgentConfig,
        sandbox_config: SandboxConfig,
        workspace_cfg: WorkspaceConfig | None = None,
    ) -> None:
        self.backend = backend
        self.agent_config = agent_config
        self.sandbox_config = sandbox_config
        self.workspace_cfg: WorkspaceConfig = workspace_cfg or WorkspaceConfig()
        self.max_turns = agent_config.max_turns
        self._approval_policy: ApprovalPolicy = get_approval_policy(
            getattr(sandbox_config, "approval_policy", "never") or "never"
        )
        self._sleep: Callable[..., Any] = asyncio.sleep

    def get_task_registry(self) -> Any | None:
        """Return the optional registry supplied by the configured backend."""
        getter = getattr(self.backend, "get_task_registry", None)
        return getter() if callable(getter) else None

    # ------------------------------------------------------------------
    # Public API — AgentTaskRunner Protocol
    # ------------------------------------------------------------------

    async def run_task(
        self,
        task: AgentTask,
        *,
        progress_callback: Any | None = None,
        diagnostics_callback: Any | None = None,
    ) -> AgentTaskResult:
        """Execute an AgentTask via the configured AgentBackend.

        Implements the ``AgentTaskRunner`` Protocol.  Builds a session
        from the task, delegates to the existing ``run()`` path, and
        returns a structured ``AgentTaskResult``.
        """
        from pathlib import Path

        from .issue import Issue
        from .workspace import Workspace

        # Build workspace + issue from the task.  ``Issue`` is retained as
        # private compatibility state for the existing SPI session runner;
        # callers of this API never need to construct or consume one.
        workspace = Workspace(
            path=Path(task.workspace_path) if task.workspace_path else Path("."),
            issue_identifier=task.context.get("issue_identifier", task.id),
            issue_id=task.context.get("issue_id", task.id),
        )
        issue = Issue(
            id=task.context.get("issue_id", task.id),
            identifier=task.context.get("issue_identifier"),
            title=task.title,
            description=task.description,
            labels=task.labels,
            url=task.context.get("issue_url"),
            state=task.context.get("issue_state"),
            author_login=task.context.get("issue_author_login"),
            branch_name=task.context.get("issue_branch_name"),
            python_executable=task.context.get("issue_python_executable", ""),
            priority=task.priority,
        )
        session = AgentSession(
            issue=issue,
            task=task,
            workspace=workspace,
            run_kind=task.kind,
            run_id=task.id,
            attempt=task.attempt,
            previous_run_ids=task.previous_run_ids,
            prompt_override=task.prompt_override,
        )

        # Build a synthetic workflow config for the run.
        import copy
        workflow = WorkflowConfig(
            agent=self.agent_config,
            sandbox=self.sandbox_config,
            workspace=self.workspace_cfg,
        )
        workflow.agent = copy.copy(self.agent_config)
        if task.max_turns is not None:
            workflow.agent.max_turns = task.max_turns
        if task.timeout_seconds is not None:
            workflow.agent.run_timeout_ms = int(task.timeout_seconds * 1000)

        bridge = _TaskProgressBridge(task.id, progress_callback)

        # Run the session using the existing path.
        await self.run(
            session,
            workflow,
            progress_reporter=bridge,
            diagnostics_callback=None,
        )

        # Build result from session state.
        result = AgentTaskResult(
            task_id=task.id,
            kind=task.kind,
            status=session.status or "completed",
            output_text=session.output_text,
            turn_count=session.turn_count,
            tool_count=session.tool_count,
            session_end_reason=session.session_end_reason,
            session_end_summary=session.session_end_summary,
            verification_status=session.verification_status,
            verification_output=session.verification_output,
            report_path=session.report_path,
            run_id=session.run_id,
            cost_usd=float(getattr(session, "cost_usd", 0.0) or 0.0),
            error=(
                session.session_end_summary or session.session_end_reason
                if session.status == "failed"
                else None
            ),
        )

        await bridge.flush()

        if diagnostics_callback is not None:
            try:
                diagnostics_callback(result)
            except Exception:
                logger.debug("diagnostics_callback failed", exc_info=True)

        return result

    # ------------------------------------------------------------------
    # Public API — mirrors AgentRunner.run() (deprecated)
    # ------------------------------------------------------------------

    async def run(
        self,
        session: AgentSession,
        workflow: WorkflowConfig,
        status_dashboard: Any | None = None,
        tracker: Any = None,
        comment_tracker: Any | None = None,
        clarification_resolver: Any | None = None,
        progress_reporter: Any | None = None,
        diagnostics_callback: Callable[[AgentSession], None] | None = None,
    ) -> None:
        """Execute one session via the configured AgentBackend.

        The session dataclass (``AgentSession`` from ``agent_runner``)
        carries mutable state that this method updates in-place:
        ``output_text``, ``turn_count``, ``tool_count``, ``status``,
        ``verification_status``, etc.
        """
        issue = session.issue
        workspace = session.workspace

        if session.run_id is None:
            session.run_id = self._build_run_id(session)

        # Stash provider/model for snapshot consumers.
        session._snapshot_provider = self.agent_config.provider or ""
        session._snapshot_model = self.agent_config.model or ""

        # Per-session NDJSON tool-event log.
        self._init_tool_event_log(session, workspace)

        # Build the prompt (returns (system_prompt_append, user_prompt)).
        prompt_parts = self._build_prompt(session, workflow, issue, workspace)
        system_prompt_append, user_prompt = prompt_parts
        system_prompt_append = self._append_skill_index(system_prompt_append)

        session._runtime_tasks = self.get_task_registry()

        # Build a SessionSpec from agent config + session context.
        spec = self._build_session_spec(session, workflow, system_prompt_append)
        session._user_prompt = user_prompt

        logger.info(
            "BackendRunner starting: backend=%s issue_id=%s run_id=%s",
            self.backend.name,
            issue.id,
            session.run_id,
        )

        try:
            await self._run_with_backend(session, spec, workflow, tracker,
                                         status_dashboard, progress_reporter)
        except Exception:
            logger.exception(
                "BackendRunner failed: backend=%s issue_id=%s run_id=%s",
                self.backend.name,
                issue.id,
                session.run_id,
            )
            session.status = "failed"
        finally:
            # Always flush telemetry after the run.
            self._telemetry_flush()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _resolve_timeouts(spec: SessionSpec) -> dict[str, float]:
        """Resolve ``None`` timeout fields to default values.

        Defaults (DESIGN_graded_timeouts_and_resume.md §1.3 / ADR-003):
          * total_timeout_s:        1800s (30 min — run watchdog)
          * handshake_timeout_s:      30s (start → first event)
          * first_turn_timeout_s:    120s (first event → first TURN_COMPLETE)
          * inactivity_timeout_s:    300s (gap between token emissions)
          * idle_watchdog_timeout_s: = total_timeout_s (no shorter than run)

        ``SessionSpec.__post_init__`` has already folded the deprecated
        ``timeout_s`` / ``stall_timeout_s`` aliases into the new fields,
        so we only need to fill in remaining gaps.

        Returns a plain dict so callers can subscript by name without
        the dataclass ceremony.
        """
        total = spec.total_timeout_s if spec.total_timeout_s is not None else 1800.0
        return {
            "total": total,
            "handshake": spec.handshake_timeout_s if spec.handshake_timeout_s is not None else 30.0,
            "first_turn": spec.first_turn_timeout_s if spec.first_turn_timeout_s is not None else 120.0,
            "inactivity": spec.inactivity_timeout_s if spec.inactivity_timeout_s is not None else 300.0,
            "idle_watchdog": (
                spec.idle_watchdog_timeout_s
                if spec.idle_watchdog_timeout_s is not None
                else total
            ),
        }

    @staticmethod
    def _build_run_id(session: AgentSession) -> str:
        """Build a stable run_id for this session."""
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        identifier = getattr(session.issue, "identifier", None) or ""
        slug = identifier.replace("/", "-").replace(" ", "_")[:40] if identifier else "unknown"
        return f"{ts}_{slug}"

    def _build_prompt(
        self,
        session: AgentSession,
        workflow: WorkflowConfig,
        issue: Issue,
        workspace: Any,
    ) -> tuple[str, str]:
        """Build the initial prompt for the agent.

        Returns (system_prompt_append, user_prompt) split by the
        USER_MESSAGE_MARKER in the workflow template.
        """
        if session.prompt_override:
            return "", session.prompt_override

        task = session.task or issue
        return PromptBuilder.render_parts(
            task,
            attempt=session.attempt,
            session=session,
            previous_run_ids=session.previous_run_ids,
            conflict_files=session.conflict_files,
        )

    @staticmethod
    def _append_skill_index(system_prompt_append: str) -> str:
        """Best-effort: append the agent-callable skill index.

        Per ``DESIGN_agent_callable_skills.md`` §3 — the agent sees a
        one-line-per-skill index and fetches full SKILL.md bodies via
        the ``load_skill`` tool. Injection must never abort a run.
        """
        try:
            from orchestratord.skills.tools import build_skill_index

            return build_skill_index(base_append=system_prompt_append)
        except Exception:
            logger.exception("skill index injection failed; continuing without skills")
            return system_prompt_append

    def _build_session_spec(
        self,
        session: AgentSession,
        workflow: WorkflowConfig,
        system_prompt: str,
    ) -> SessionSpec:
        """Build a SessionSpec from agent config and session context."""
        # Expose the agent-callable skill tool to every backend
        # (DESIGN_agent_callable_skills.md §3.2): append ``load_skill`` to
        # the allow-list when one is configured, and publish the tool
        # description via the opaque ``extra`` channel so backends that
        # control their own tool surface can forward it.
        tools_allow_cfg = getattr(self.agent_config, "tools_allow", None) or None
        if tools_allow_cfg is not None:
            tools_allow: list[str] | None = list(tools_allow_cfg)
            if "load_skill" not in tools_allow:
                tools_allow.append("load_skill")
        else:
            tools_allow = None
        extra: dict[str, Any] = (
            {"runtime_tasks": session._runtime_tasks}
            if session._runtime_tasks is not None
            else {}
        )
        try:
            from orchestratord.skills.tools import skill_tool_descriptions

            skill_tools = skill_tool_descriptions()
            if skill_tools:
                extra["skill_tools"] = skill_tools
        except Exception:
            logger.exception("skill tool description injection failed; continuing")
        return SessionSpec(
            cwd=str(session.workspace.path),
            system_prompt=system_prompt or None,
            model=self.agent_config.model or None,
            provider=self.agent_config.provider or None,
            base_url=getattr(self.agent_config, "base_url", None) or None,
            api_key=getattr(self.agent_config, "api_key", None) or None,
            cordis=getattr(self.agent_config, "cordis", None) or None,
            runtime_bin=getattr(self.agent_config, "runtime_bin", None) or None,
            permission_mode=self.agent_config.permission_mode or None,
            tools_allow=tools_allow,
            tools_deny=getattr(self.agent_config, "tools_deny", []) or [],
            env=self._build_env(session),
            resume_session_id=session.run_id or None,
            max_turns=self.max_turns,
            extra=extra,
        )

    def _build_env(self, session: AgentSession) -> dict[str, str]:
        """Build environment variables for the backend session."""
        env: dict[str, str] = {}
        # Merge agent-level env from workflow config
        agent_env = getattr(self.agent_config, "env", None) or {}
        env.update(agent_env)
        return env

    @staticmethod
    def _init_tool_event_log(session: AgentSession, workspace: Any) -> None:
        """Initialize the per-session NDJSON tool-event log."""
        if session.tool_events_path is None and session.run_id:
            reports_dir = os.path.join(str(workspace.path), ".reports")
            os.makedirs(reports_dir, exist_ok=True)
            session.tool_events_path = os.path.join(
                reports_dir, f"{session.run_id}.events.ndjson"
            )

    # ------------------------------------------------------------------
    # Core execution
    # ------------------------------------------------------------------

    async def _run_with_backend(
        self,
        session: AgentSession,
        spec: SessionSpec,
        workflow: WorkflowConfig,
        tracker: Any,
        status_dashboard: Any | None,
        progress_reporter: Any | None,
    ) -> None:
        """Drive the AgentBackend session and process its event stream."""
        backend_caps = self.backend.capabilities()
        logger.info(
            "BackendRunner capabilities: %s",
            {f: getattr(backend_caps, f) for f in [
                "streaming_deltas", "resumable", "interrupt",
                "approval_hooks", "parallel_sessions", "cost_reporting",
                "tool_filtering", "takeover",
            ]},
        )

        # The control socket is an optional observability/control surface.
        # Its failure must never prevent the backend from running.
        owns_control_socket = False
        if session.control_socket is None:
            try:
                control_dir = session.workspace.path / ".run_control"
                is_windows = os.name == "nt"
                sock_path = None if is_windows else control_dir / f"{session.run_id}.sock"
                control_socket = ControlSocket(sock_path, tcp=is_windows)
                await control_socket.start()
                session.control_socket = control_socket
                session.control_socket_path = control_socket.endpoint
                # TCP endpoints have ephemeral ports, so a separately
                # launched dashboard needs a local discovery record.
                if is_windows:
                    control_dir.mkdir(parents=True, exist_ok=True)
                    endpoint_file = control_dir / f"{session.run_id}.endpoint.json"
                    endpoint_file.write_text(
                        json.dumps({"endpoint": control_socket.endpoint}), encoding="utf-8"
                    )
                owns_control_socket = True
            except Exception:
                logger.debug("control socket unavailable for run_id=%s", session.run_id, exc_info=True)

        # Create the SPI session.
        spi_session = self.backend.create_session(spec)
        session_context = {
            "issue_id": session.issue.id,
            "workspace_path": str(session.workspace.path),
            "run_id": session.run_id,
            "permission_mode": self.agent_config.permission_mode,
        }

        # ADR-003 §2.5: probe resume status before any send().
        if spec.resume_session_id:
            probe_result = await self._probe_resume_or_log(spi_session, spec)
            if probe_result is ResumeStatus.REJECTED:
                # Backend explicitly refused the resume target. Emit a
                # structured ERROR and skip send() so the orchestrator's
                # normal failure path can format a user-facing message.
                logger.warning(
                    "BackendRunner: resume rejected session_id=%s backend=%s",
                    spec.resume_session_id,
                    self.backend.name,
                )
                session.status = "failed"
                session.session_end_reason = "resume_rejected"
                session.session_end_summary = (
                    f"Resume target session {spec.resume_session_id} was "
                    f"explicitly rejected by {self.backend.name}"
                )
                try:
                    await spi_session.close()
                except Exception:
                    logger.debug("spi_session.close() failed", exc_info=True)
                return

        # ADR-003 §3.2: resolve and surface the 5-level timeout bundle.
        timeouts = self._resolve_timeouts(spec)
        logger.info(
            "BackendRunner timeouts: backend=%s total=%.1fs handshake=%.1fs "
            "first_turn=%.1fs inactivity=%.1fs idle_watchdog=%.1fs",
            self.backend.name,
            timeouts["total"],
            timeouts["handshake"],
            timeouts["first_turn"],
            timeouts["inactivity"],
            timeouts["idle_watchdog"],
        )

        try:
            # Send the prompt and start processing events.
            await spi_session.send(getattr(session, "_user_prompt", "") or "")
            await self._process_events(
                spi_session, session, session_context,
                workflow, tracker, status_dashboard, progress_reporter,
                timeouts=timeouts,
            )
        finally:
            try:
                await spi_session.close()
            except Exception:
                logger.debug("spi_session.close() failed", exc_info=True)
            if owns_control_socket and session.control_socket is not None:
                try:
                    await session.control_socket.stop()
                except Exception:
                    logger.debug("control_socket.stop() failed", exc_info=True)
                session.control_socket = None
                endpoint_file = session.workspace.path / ".run_control" / f"{session.run_id}.endpoint.json"
                try:
                    endpoint_file.unlink(missing_ok=True)
                except OSError:
                    logger.debug("control endpoint cleanup failed", exc_info=True)

    @staticmethod
    async def _probe_resume_or_log(
        spi_session: Any, spec: SessionSpec
    ) -> ResumeStatus:
        """Invoke ``probe_resume()`` defensively and log the result.

        Returns the ResumeStatus as-is (including UNDETECTABLE). The
        caller (``_run_with_backend``) is responsible for branching
        only on REJECTED; UNDETECTABLE falls through to the normal
        ``send()`` path.
        """
        probe = getattr(spi_session, "probe_resume", None)
        if probe is None:
            logger.debug(
                "BackendRunner: spi_session does not implement probe_resume "
                "— defaulting to UNDETECTABLE"
            )
            return ResumeStatus.UNDETECTABLE
        try:
            return await probe()
        except Exception as exc:
            logger.warning(
                "BackendRunner: probe_resume raised %s — treating as UNDETECTABLE",
                exc,
            )
            return ResumeStatus.UNDETECTABLE

    async def _process_events(
        self,
        spi_session: Any,  # AgentSession Protocol
        session: AgentSession,
        session_context: dict[str, Any],
        workflow: WorkflowConfig,
        tracker: Any,
        status_dashboard: Any | None,
        progress_reporter: Any | None,
        *,
        timeouts: dict[str, float] | None = None,
    ) -> None:
        """Process the EventEnvelope stream from the SPI session.

        ``timeouts`` carries the 5-level resolved timeout bundle from
        :meth:`_resolve_timeouts`. When supplied, this coroutine
        enforces ``idle_watchdog_timeout_s`` (max session idle gap)
        and ``total_timeout_s`` (run-level watchdog). Individual
        phase timeouts (handshake / first_turn / inactivity) are
        enforced inside the event loop below.
        """
        consecutive_noop_turns = 0
        last_file_status_snapshot: dict[str, Any] | None = None

        run_start = time.monotonic()
        last_event_monotonic = run_start
        handshake_complete = False
        first_turn_complete = False
        inactivity_threshold: float | None = (
            timeouts["inactivity"] if timeouts else None
        )
        idle_threshold: float | None = (
            timeouts["idle_watchdog"] if timeouts else None
        )
        total_threshold: float | None = (
            timeouts["total"] if timeouts else None
        )
        handshake_threshold: float | None = (
            timeouts["handshake"] if timeouts else None
        )
        first_turn_threshold: float | None = (
            timeouts["first_turn"] if timeouts else None
        )

        async for event in spi_session.events():
            # Commands are drained before handling the event so an operator
            # request arriving at a turn boundary is available immediately.
            if _drain_control_commands(session):
                session.status = "failed"
                break
            kind = event.kind
            payload = event.payload

            if kind == EventKind.TEXT:
                text = payload.get("text", "")
                if text:
                    session.output_text += text
                if progress_reporter is not None and hasattr(progress_reporter, "on_text"):
                    progress_reporter.on_text(text)

            elif kind == EventKind.TEXT_DELTA:
                text = payload.get("text", "") or payload.get("delta", "")
                if text:
                    session.output_text += text
                if progress_reporter is not None and hasattr(progress_reporter, "on_text_delta"):
                    progress_reporter.on_text_delta(text)

            elif kind == EventKind.TOOL_CALL:
                session.tool_count += 1
                self._handle_tool_call_envelope(event, session_context)
                if progress_reporter is not None and hasattr(progress_reporter, "on_tool_call"):
                    progress_reporter.on_tool_call(
                        payload.get("name", "unknown"),
                        payload.get("call_id", ""),
                    )

            elif kind == EventKind.TOOL_RESULT:
                if progress_reporter is not None and hasattr(progress_reporter, "on_tool_result"):
                    progress_reporter.on_tool_result(
                        payload.get("call_id", ""),
                    )

            elif kind == EventKind.TURN_COMPLETE:
                session.turn_count += 1
                # Check for noop (no file changes).
                file_changed = await self._check_file_changes(
                    session, last_file_status_snapshot
                )
                if file_changed:
                    consecutive_noop_turns = 0
                else:
                    consecutive_noop_turns += 1

                if consecutive_noop_turns >= _NOOP_DETECTION_MAX_TURNS:
                    logger.warning(
                        "Noop detection: %d consecutive turns without file changes",
                        consecutive_noop_turns,
                    )
                    session.status = "completed"
                    session.session_end_reason = "noop_detected"
                    break

                if progress_reporter is not None and hasattr(progress_reporter, "on_turn_complete"):
                    progress_reporter.on_turn_complete(
                        TurnComplete(turn=session.turn_count), session
                    )

                # SPI sessions are bidirectional.  At a turn boundary,
                # deliver all queued follow-ups as a single explicit
                # operator message.  This works for every backend without
                # replacing the original task prompt.
                pending_followups = getattr(session, "_pending_followups", None) or []
                if pending_followups:
                    followups = "\n".join(f"- {message}" for message in pending_followups)
                    followup_prompt = f"[Operator follow-up]\n{followups}"
                    try:
                        await spi_session.send(followup_prompt)
                    except Exception:
                        logger.exception("followup delivery failed run_id=%s", session.run_id)
                    else:
                        pending_followups.clear()

                # Check if issue is still active via tracker.
                if tracker is not None and not await self._should_continue(
                    session, tracker
                ):
                    session.status = "completed"
                    break

            elif kind == EventKind.SESSION_COMPLETE:
                reason = payload.get("reason", "success")
                session.status = "completed" if reason in ("success", "turn_complete") else "failed"
                session.session_end_reason = reason
                if progress_reporter is not None and hasattr(progress_reporter, "on_session_complete"):
                    progress_reporter.on_session_complete(
                        SessionComplete(reason=reason), session
                    )
                # ``break`` below would bypass the common broadcast tail.
                # Emit the terminal lifecycle frame explicitly so connected
                # chat clients can settle the final assistant bubble before
                # the socket closes and they receive RunEnded.
                await _broadcast_to_socket(session, event)
                break

            elif kind == EventKind.ERROR:
                error_msg = payload.get("message", "unknown error")
                logger.error("BackendRunner event error: %s", error_msg)
                if progress_reporter is not None and hasattr(progress_reporter, "on_error"):
                    progress_reporter.on_error(error_msg)

            # Mark phase completion for the 5-level timeout watchdog.
            if not handshake_complete:
                handshake_complete = True
            if kind == EventKind.TURN_COMPLETE and not first_turn_complete:
                first_turn_complete = True

            # ADR-003 §3.2: enforce per-phase and run-level timeouts.
            now = time.monotonic()
            last_event_monotonic = now
            elapsed = now - run_start
            gap = now - last_event_monotonic  # ==0 right after event; reset below
            if total_threshold is not None and elapsed >= total_threshold:
                logger.error(
                    "BackendRunner total_timeout exceeded (%.1fs ≥ %.1fs)",
                    elapsed, total_threshold,
                )
                session.session_end_reason = "total_timeout"
                session.status = "failed"
                break
            if idle_threshold is not None and gap >= idle_threshold:
                logger.error(
                    "BackendRunner idle_watchdog exceeded (%.1fs ≥ %.1fs)",
                    gap, idle_threshold,
                )
                session.session_end_reason = "idle_watchdog_timeout"
                session.status = "failed"
                break

            # Broadcast to control socket if active.
            await _broadcast_to_socket(session, event)

            # Drain control commands.
            if _drain_control_commands(session):
                session.status = "failed"
                break

    # ------------------------------------------------------------------
    # Tool call handling
    # ------------------------------------------------------------------

    def _handle_tool_call_envelope(
        self,
        event: EventEnvelope,
        session_context: dict[str, Any],
    ) -> None:
        """Apply approval policy to a TOOL_CALL EventEnvelope."""
        payload = event.payload
        policy_event = ToolCallEvent(
            tool_name=payload.get("name", "unknown"),
            params=payload.get("arguments", {}),
            tool_use_id=payload.get("call_id"),
        )
        self._approval_policy.evaluate(policy_event, session_context)

        if policy_event.is_approved is False:
            logger.warning(
                "Tool call denied: %s reason=%s",
                policy_event.tool_name,
                policy_event._deny_reason,
            )

    # ------------------------------------------------------------------
    # File change detection
    # ------------------------------------------------------------------

    async def _check_file_changes(
        self,
        session: AgentSession,
        last_snapshot: dict[str, Any] | None,
    ) -> bool:
        """Check whether any files have changed since the last snapshot."""
        try:
            from orchestratord.git_utils import get_file_status

            current = get_file_status(str(session.workspace.path))
            current_map = {s.path: s for s in current}
            if last_snapshot is None:
                return any(
                    s.status not in ("unmodified", "ignored")
                    for s in current
                )
            # Compare with previous snapshot.
            for path, status in current_map.items():
                prev = last_snapshot.get(path)
                if prev is None or prev.status != status.status:
                    return True
            return False
        except Exception:
            logger.debug("File change check failed", exc_info=True)
            return True  # Assume changed on error

    # ------------------------------------------------------------------
    # Continuation check
    # ------------------------------------------------------------------

    async def _should_continue(
        self,
        session: AgentSession,
        tracker: Any,
    ) -> bool:
        """Check with the tracker whether the issue is still active."""
        try:
            issue_id = session.issue.id
            if not issue_id:
                return True
            states = await tracker.fetch_issue_states_by_ids([issue_id])
            state = states.get(issue_id)
            if state is None:
                return True
            return state.get("active", True)
        except Exception:
            logger.debug("should_continue check failed", exc_info=True)
            return True

    # ------------------------------------------------------------------
    # Telemetry
    # ------------------------------------------------------------------

    @staticmethod
    def _telemetry_flush() -> None:
        """Flush telemetry after a run, best-effort."""
        try:
            from telemetry.recorder import get_recorder

            recorder = get_recorder()
            if not getattr(recorder, "enabled", False):
                return
            if not recorder.config.reporting.reporting_enabled:
                return
            recorder.flush()
        except Exception:
            pass
