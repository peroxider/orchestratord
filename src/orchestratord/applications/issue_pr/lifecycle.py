"""IssueToPrLifecycle — Application 协议的 issue→PR 业务生命周期（DESIGN §4.2）。

C2b 形态：业务装配（``prepare_launch``）、会话装饰（``decorate_session``）、
viz 后业务门（``post_viz_gate``）、结果解释（``interpret`` /
``interpret_sync_result``）与业务控制命令注册表（``control_commands``）
自宿主（Orchestrator）``_launch_issue`` / ``_run_issue`` /
``_process_control_commands`` 机械迁入——两侧 strip 归一 +
``self.``→``self._host.`` 改写后逐行等值。``_launch_issue`` 业务段含
副作用早退（claimed.discard / completed.add / followup 已处理即 return）
且 viz journal 楔在模式选择与 followup 检查之间，单一 prepare_run 无法
保序 → **3-seam 保序委托**：宿主壳 ``_launch_issue`` 依次调
prepare_launch → 建 AgentSession（机制）→ decorate_session → viz
journal（机制）→ post_viz_gate → 机制尾；``_run_issue`` 的终态映射链
整体迁 ``interpret``，workspace 清理与 claimed 释放为机制段留宿主。

interim 签名偏差（设计文档进度表已记录）：协议 ``prepare_run(item, ctx)``
/ ``interpret_result(item, result, ctx)`` 依赖 RunContext 与 AgentTask /
AgentTaskResult 物化，而 RunContext 现仅存在于 backend_runner.run_task
帧、AgentSession 无 run_context 字段——故取业务直参（issue / session），
待 Kernel dispatch-loop 重构切片升级为协议全签名。Outcome 返回值本切片
仅归位（Kernel 端消费随 dispatch-loop 切片）。

模块定位偏差（任务 #11 预案 ⚠️ 的落地）：本类落独立模块而非 app.py——
app.py 顶层 import orchestration_subsystem（cli/server 的子系统子类壳），
宿主 __init__ 需要急切可达的本模块若经 app.py 会把真实
OrchestrationSubsystem 冻结进类继承（test_im_events 经 monkeypatch
``orchestration_subsystem.OrchestrationSubsystem`` 换桩，依赖该 seam 的
惰性解析）。本模块顶层只 import kernel + 低层模块，可被宿主安全急切
加载。C2c 装配进 orchestration_subsystem；P6 与 IssueToPrApplication
定名归并。

宿主经结构化协议引用；本模块不得在模块顶层 import orchestrator/
orchestration_subsystem/app——组合根 orchestrator.py 顶层 import 本包
（prompts 注册级联），反向依赖会成环（import 时序契约见
``applications/issue_pr/__init__.py`` 说明）。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from orchestratord.events import EventLevel
from orchestratord.kernel.application import CommandHandler, Outcome, PreparedRun
from orchestratord.kernel.events import KernelEvent, KernelEventKind
from orchestratord.kernel.run_context import RunContext
from orchestratord.modes.base import DEFAULT_MODE, ModeDecision
from orchestratord.tracker import Intent, PullRequestCapability, supports
from .commands import IssuePrCommands
from .interpret import IssuePrInterpretation
from .payloads import session_payload

if TYPE_CHECKING:
    from orchestratord.config.schema import WorkflowConfig
    from orchestratord.git.sync import GitSyncResult, GitSyncService
    from orchestratord.issue_registry import IssueRegistry
    from orchestratord.issue_registry.issue import Issue
    from orchestratord.kernel.dispatch import OrchestratorState
    from orchestratord.mode_selector import ModeSelector
    from orchestratord.session_state import AgentSession
    from orchestratord.status_dashboard import StatusDashboard
    from orchestratord.tracker import TrackerAdapter
    from orchestratord.workspace import WorkspaceManager

logger = logging.getLogger(__name__)


class _IssueLifecycleHost(Protocol):
    """宿主（Orchestrator）暴露给 IssueToPrLifecycle 的最小生命周期接缝。"""

    tracker: TrackerAdapter
    workflow: WorkflowConfig
    git_sync: GitSyncService
    workspace: WorkspaceManager
    _registry: IssueRegistry
    _state: OrchestratorState
    _workspace_root: Path
    _mode_selector: ModeSelector
    status_dashboard: StatusDashboard

    def _emit_im_event(
        self,
        issue_id: str,
        event_type: str,
        level: EventLevel,
        message: str = "",
        payload: dict[str, Any] | None = None,
    ) -> None: ...

    async def _schedule_retry(
        self,
        session: AgentSession,
        *,
        delay_base_ms: int | None = None,
    ) -> bool: ...



class IssueToPrLifecycle:
    """Issue→PR application lifecycle attached to the generic Kernel."""

    name = "issue_pr"

    def __init__(self, host: _IssueLifecycleHost | None = None) -> None:
        # C2c 两段式装配（DESIGN §4.2/G2）：组合根可先以无宿主形态构造
        # （宿主彼时尚未存在），由 Orchestrator.__init__ 回绑 ``_host``。
        self._host = host
        self._interpretation = IssuePrInterpretation(host)
        self._commands = IssuePrCommands(host)

    def bind_host(self, host: _IssueLifecycleHost) -> None:
        """Complete two-phase composition-root wiring."""
        self._host = host
        self._interpretation.host = host
        self._commands.host = host

    def work_provider(self) -> Any:
        """Return the provider assembled by the composition root."""
        return getattr(self._host, "_work_provider", None)

    def prompt_profiles(self) -> dict[str, str]:
        """Issue→PR prompt profiles are registered by the prompt module."""
        return {}

    async def prepare_run(self, item: Any, ctx: RunContext) -> PreparedRun | None:
        """Full-signature Application seam.

        The issue workspace and registry gates are application decisions;
        this adapter exposes them through the full Kernel protocol.
        """
        issue = item.business.get("issue") if item is not None else None
        if issue is None:
            raise ValueError("issue_pr prepare_run requires WorkItem.business['issue']")
        prepared = await self.prepare_launch(issue)
        if prepared is None:
            return None
        return prepared

    async def interpret_result(
        self, item: Any, result: Any, ctx: RunContext
    ) -> Outcome:
        """Turn a runner result into the application-owned Outcome."""
        issue = item.business.get("issue") if item is not None else None
        if issue is None:
            return Outcome.dispose("missing_issue")
        from orchestratord.session_state import AgentSession

        session = AgentSession(
            subject=issue,
            workspace=ctx.workspace,
            task=ctx.task,
            run_context=ctx,
            run_id=getattr(result, "run_id", None) or ctx.run_id,
            conversation_id=getattr(result, "conversation_id", None) or ctx.conversation_id,
            status=getattr(result, "status", "failed"),
            output_text=getattr(result, "output_text", ""),
            turn_count=getattr(result, "turn_count", 0),
            tool_count=getattr(result, "tool_count", 0),
            session_end_reason=getattr(result, "session_end_reason", None),
            session_end_summary=getattr(result, "session_end_summary", ""),
            verification_status=getattr(result, "verification_status", None),
            verification_output=getattr(result, "verification_output", None),
            report_path=getattr(result, "report_path", None),
        )
        session.business.update(ctx.business)
        return await self.interpret(session)

    async def execute_session(self, session: AgentSession) -> None:
        """Own the issue-specific execution adapter during the transition.

        The host callback is deliberately a single opaque execution seam;
        Kernel never sees issue/task mapping, reproduction gates, premise
        checks, or git workspace policy.
        """
        from .runner import run_issue_body

        await run_issue_body(self._host, session)

    async def on_kernel_event(self, event: KernelEvent) -> None:
        """Run the issue-specific periodic loops on Kernel ``POLL_TICK``."""
        if event.kind is not KernelEventKind.POLL_TICK or self._host is None:
            return
        phase = event.payload.get("phase", "all")
        if phase in ("before_retry", "all"):
            await self._host._clarification_resolver.poll_clarification_answers()
        if phase not in ("after_retry", "all"):
            return
        await self._interpretation._process_escalated_issues()
        await self._interpretation._process_review_feedback()
        await self._interpretation._process_pending_rebase_conflicts()
        await self._interpretation._process_pr_conflict_scan()

    async def prepare_launch(self, issue: Issue) -> PreparedRun | None:
        """执行前业务装配（协议 prepare_run 的 interim 直参形态）。

        C2b 自宿主 ``_launch_issue``（pre-image :2348-2521）机械迁入：
        依赖复检、intent reset、branch 推导、workspace 创建（§4.2
        「workspace 分支准备」交织点的消解）、registry 记账、tracker
        刷新守卫。``None`` = gated / skip（既有早退路径，副作用
        claimed.discard / completed.add 已随迁）；非 None 时宿主续建
        AgentSession 并按 3-seam 保序委托推进。
        """
        if not await self._interpretation._dependencies_satisfied(issue):
            self._host._state.claimed.discard(issue.id)
            return None

        # If the registry carries a RETRY intent for this
        # issue, close the existing remote PR (best-effort) and reset
        # the local record so the new run starts from a clean slate.
        # This must happen BEFORE workspace creation so the new run
        # does not try to push a follow-up commit to a closed PR.
        await self._interpretation._prepare_intent_reset(issue)

        workspace_strategy = self._host.workflow.workspace.strategy
        branch_name = getattr(issue, "branch_name", None)
        if not branch_name:
            branch_name = self._host.git_sync._default_branch_name(issue)
            issue.branch_name = branch_name

        try:
            workspace = await self._host.workspace.create_for_issue(issue)
        except Exception as exc:
            logger.error(
                "Workspace creation failed issue_id=%s: %s",
                issue.id,
                exc,
            )
            self._host._state.claimed.discard(issue.id)
            return None

        # Register as pending so restart won't re-launch this issue
        base_branch = (
            getattr(issue, "base_branch", None)
            or self._host.workflow.workspace.base_branch
            or "main"
        )
        integration_branch = self._host.workflow.workspace.integration_branch
        if workspace_strategy == "sequential" and integration_branch:
            branch_name = integration_branch
        start_commit_sha = await self._host.workspace.current_head(workspace.path)
        base_commit_sha = start_commit_sha if workspace_strategy == "sequential" else None
        previous_issue_id = None
        sequence_index = None
        if workspace_strategy == "sequential":
            previous_record = self._host._registry.latest_sequential_record()
            previous_issue_id = previous_record.issue_id if previous_record else None
            sequence_index = (previous_record.sequence_index or 0) + 1 if previous_record else 1
        # In sequential mode the registry's workspace_path must
        # record the configured root (not whatever WorkspaceManager
        # happened to return for the current issue), so that subsequent
        # issues can resolve the previous commit chain against the same
        # path. In isolated / shared modes the per-issue workspace.path
        # is already the canonical location, so keep that.
        recorded_workspace_path = (
            str(self._host._workspace_root) if workspace_strategy == "sequential" else str(workspace.path)
        )
        existing_record = self._host._registry.get(issue.id or "")
        parent_run_id = (
            existing_record.run_id
            if existing_record is not None and existing_record.run_id
            else (existing_record.previous_run_ids[-1] if existing_record and existing_record.previous_run_ids else None)
        )
        record = self._host._registry.register(
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
            refreshed = await self._host.tracker.fetch_issue_states_by_ids([issue.id])
            refreshed_issue = refreshed.get(issue.id)
            if refreshed_issue is None:
                logger.info("Issue %s no longer exists, skipping", issue.id)
                self._host._state.claimed.discard(issue.id)
                return None
            active_states = [
                s.strip().lower() for s in (getattr(self._host.tracker, "active_states", None) or [])
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
                self._host._state.claimed.discard(issue.id)
                return None
            # Check for existing PR (only for repository-backed trackers)
            branch_name = refreshed_issue.branch_name
            if branch_name and supports(self._host.tracker, PullRequestCapability):
                base_branch = getattr(refreshed_issue, "base_branch", "main") or "main"
                existing_pr = await self._host.tracker.find_pull_request(
                    head_branch=branch_name,
                    base_branch=base_branch,
                )
                if existing_pr is not None:
                    # Explicit follow-up and retry intents both bypass the
                    # ordinary existing-PR guard. Follow-up reuses the PR;
                    # retry already attempted to close it and must still
                    # proceed when that best-effort close was a no-op.
                    record = self._host._registry.get(issue.id or "")
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
                        self._host._state.claimed.discard(issue.id)
                        # Also add to completed so we don't re-process after restart
                        self._host._state.completed.add(issue.id)
                        return None

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
            if self._host._registry.has_pr(issue.id or "") or self._host._registry.is_terminal(issue.id or ""):
                # Explicit retry/follow-up intents deliberately bypass the
                # handled guard. Retry clears stale PR state before reaching
                # this point; follow-up reuses it.
                record = self._host._registry.get(issue.id or "")
                if record and record.intent in (Intent.RETRY, Intent.FOLLOWUP):
                    logger.info(
                        "Issue %s %s intent bypasses registry guard "
                        "(has_pr=%s, is_terminal=%s), proceeding",
                        issue.id,
                        record.intent.value,
                        self._host._registry.has_pr(issue.id or ""),
                        self._host._registry.is_terminal(issue.id or ""),
                    )
                else:
                    logger.info(
                        "Issue %s already handled (registry: has_pr=%s, "
                        "is_terminal=%s), skipping via _launch_issue guard",
                        issue.id,
                        self._host._registry.has_pr(issue.id or ""),
                        self._host._registry.is_terminal(issue.id or ""),
                    )
                    self._host._state.claimed.discard(issue.id)
                    self._host._state.completed.add(issue.id)
                    return None

            # Update issue with latest state
            issue.state = refreshed_issue.state
        except Exception as exc:
            logger.warning(
                "Could not verify issue state for %s: %s — proceeding anyway",
                issue.id,
                exc,
            )

        return PreparedRun(
            business={
                "workspace": workspace,
                "record": record,
                "conversation_id": record.conversation_id,
                "parent_run_id": parent_run_id,
                "workspace_strategy": workspace_strategy,
                "base_branch": base_branch,
                "integration_branch": integration_branch,
                "start_commit_sha": start_commit_sha,
                "base_commit_sha": base_commit_sha,
                "previous_issue_id": previous_issue_id,
                "sequence_index": sequence_index,
            },
        )

    async def decorate_session(
        self, session: AgentSession, prepared: PreparedRun
    ) -> ModeDecision:
        """会话装饰：clarification 注入、retry 上下文、协作模式选择。

        C2b 自宿主 ``_launch_issue``（pre-image :2542-2593）机械迁入。
        返回 ``mode_decision``——宿主 viz journal 的 phase 事件原直读该
        局部变量，3-seam 拆分后经本返回值转交（声明适配）。
        """
        issue = session.issue
        business = prepared.business
        workspace = business["workspace"]
        workspace_strategy = business["workspace_strategy"]
        base_branch = business["base_branch"]
        integration_branch = business["integration_branch"]
        start_commit_sha = business["start_commit_sha"]
        base_commit_sha = business["base_commit_sha"]
        previous_issue_id = business["previous_issue_id"]
        sequence_index = business["sequence_index"]
        clarification_record = self._host._registry.get(issue.id or "")
        if clarification_record is not None and clarification_record.local_answer:
            session.clarification_answer = clarification_record.local_answer
            session.clarification_source = clarification_record.local_answer_source
            if clarification_record.question_history:
                session.clarification_question = "\n".join(
                    f"- {question}" for question in clarification_record.question_history
                )
        retry_attempt = self._host._state.retry_attempts.get(issue.id or "", 0)
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
            mode_decision = self._host._mode_selector.choose(issue)
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
        record = self._host._registry.get(issue.id or "")
        if record is not None:
            record.collaboration_mode = mode_decision.mode
            record.mode_decision_reason = mode_decision.reason
            record.touch()
            self._host._registry._save()
        logger.info(
            "Issue %s collaboration_mode=%s (source=%s, reason=%s)",
            issue.id,
            mode_decision.mode,
            mode_decision.source,
            mode_decision.reason,
        )
        return mode_decision

    async def post_viz_gate(self, session: AgentSession) -> bool:
        """viz journal 之后的业务门（3-seam 第三缝）。

        C2b 自宿主 ``_launch_issue``（pre-image :2609-2639）机械迁入：
        review-feedback followup 早退、``_prepare_intent_session``、
        stage_id / previous_run_ids / previous_verification_error 注入。
        ``False`` = 已处理 / 跳过（宿主弃置 session，不进入机制尾）。
        """
        issue = session.issue
        # Command/review FOLLOWUP intents fetch pending PR feedback. Dashboard
        # conversation turns remain agent_followup runs so operator text is not
        # discarded merely because there is no pending PR review.
        followup_record = self._host._registry.get(issue.id or "")
        if self._interpretation._uses_review_feedback_followup(followup_record):
            followup_handled = await self._interpretation._launch_followup_with_pending_reviews(issue)
            if not followup_handled:
                logger.info(
                    "Issue %s follow-up: no pending review feedback to process — skip",
                    issue.id or "",
                )
            return False
        # If the registry intent is FOLLOWUP, wire the
        # session so the agent + git_sync know to reuse the existing
        # branch / PR rather than create a new run.
        self._interpretation._prepare_intent_session(session)
        if session.run_kind == "review_followup":
            session.stage_id = "review_followup"
        # Retry context: propagate previous_run_ids from the registry
        # to the session so the prompt builder can inject them.
        prev_record = self._host._registry.get(issue.id or "")
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
        return True

    async def interpret_sync_result(
        self, session: AgentSession, sync_result: GitSyncResult | None
    ) -> bool:
        """git_sync 结果分类（协议 interpret 家族的同步结果前置段）。

        C2b 自宿主 ``_run_issue``（pre-image :3194-3213）机械迁入：
        daemon 触发 read-only loop / stagnation 等终止场景时 git_sync
        不创建 PR 并标记 empty_branch_no_commits——不能走 mark_synced
        （会标 SYNCED + 无 PR），必须置失败态让终态链走 mark_failed。
        ``True`` = 已分类（宿主提前结束本轮 run）。
        """
        if sync_result is None or sync_result.session_end_reason != "empty_branch_no_commits":
            return False
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
        return True

    async def interpret(self, session: AgentSession) -> Outcome:
        """执行后业务解释（协议 interpret_result 的 interim 直参形态）。

        C2b 自宿主 ``_run_issue`` 终态映射链（pre-image :3668-3914）机械
        迁入：session 机制态 → registry 状态机 + tracker 同步 + IM 通知 +
        run summary comment。返回 Outcome 供 Kernel 消费（本切片仅归位，
        Kernel 端消费随 dispatch-loop 切片）；workspace 清理（保全策略）
        与 claimed 释放为机制段，留宿主 finally。
        """
        outcome: Outcome
        # Review gate: if the issue is already in pending_review
        # (set by the early return above), skip the final status
        # transition so the outer finally does NOT overwrite it with
        # COMPLETED. The human must run `orchestrator issue review
        # --id ... --approve` to move it to COMPLETED.
        if session.issue.id in self._host._state.pending_review:
            # Issue is waiting for human review — do nothing further.
            # Workspace preservation is handled by the early return.
            logger.info(
                "Issue %s left in pending_review state — human review required",
                session.issue.id,
            )
            outcome = Outcome.wait_external("pending_review")
        elif session.status == "completed":
            self._host.status_dashboard.on_session_complete(session.issue.id or "")
            self._host._emit_im_event(
                session.issue.id or "",
                "issue.completed",
                EventLevel.SUCCESS,
                "任务完成",
                session_payload(self._host.tracker, self._host._registry, session),
            )
            self._host._state.completed.add(session.issue.id or "")
            self._host._registry.mark_completed(session.issue.id or "")
            await self._interpretation._sync_tracker_issue_state(session.issue.id or "", "completed")
            outcome = Outcome.dispose("completed")
        elif session.status == "verification_failed":
            self._host.status_dashboard.on_session_failed(
                session.issue.id or "",
                str(session.status),
            )
            # Terminal IM event already emitted by the originating
            # except handler (VerificationFailed / HookFailedError /
            # GitSyncPostCommitError). ``verification_failed`` is
            # only ever set there, so re-emitting here would double
            # (e.g. ``post_commit_failed`` ERROR then
            # ``verification.failed`` WARN). See review 🟡2.
            self._host._registry.mark_verification_failed(
                session.issue.id or "",
                output=getattr(session, "verification_output", None),
                hook_error=getattr(session, "last_hook_error", None),
            )
            # Gate the tracker close on the retry outcome: a
            # pending retry keeps the issue open on the tracker
            # (GitCode cannot reopen a closed issue).
            retry_scheduled = await self._host._schedule_retry(session)
            if not retry_scheduled:
                await self._interpretation._sync_tracker_issue_state(
                    session.issue.id or "", "verification_failed"
                )
            outcome = (
                Outcome.retry(reason="verification_failed")
                if retry_scheduled
                else Outcome.dispose("verification_failed")
            )
        elif session.status == "agent_timeout":
            self._host.status_dashboard.on_session_failed(
                session.issue.id or "",
                str(session.status),
            )
            # Terminal IM event already emitted by the
            # ``asyncio.TimeoutError`` except handler; ``agent_timeout``
            # is only ever set there. See review 🟡2.
            self._host._registry.mark_failed_with_reason(
                session.issue.id or "",
                getattr(session, "last_hook_error", None)
                or getattr(session, "verification_output", None)
                or "Agent run timed out",
            )
            retry_scheduled = await self._host._schedule_retry(session)
            if not retry_scheduled:
                await self._interpretation._sync_tracker_issue_state(session.issue.id or "", "failed")
            outcome = (
                Outcome.retry(reason="agent_timeout")
                if retry_scheduled
                else Outcome.dispose("agent_timeout")
            )
        elif session.status == "max_turns_exceeded":
            self._host.status_dashboard.on_session_failed(
                session.issue.id or "",
                str(session.status),
            )
            self._host._emit_im_event(
                session.issue.id or "",
                "agent.max_turns_exceeded",
                EventLevel.WARN,
                "max turns exceeded",
            )
            self._host._registry.mark_failed(session.issue.id or "")
            retry_scheduled = await self._host._schedule_retry(
                session,
                delay_base_ms=self._host.workflow.agent.max_turns_retry_delay_ms,
            )
            if not retry_scheduled:
                await self._interpretation._sync_tracker_issue_state(session.issue.id or "", "failed")
            outcome = (
                Outcome.retry(reason="max_turns_exceeded")
                if retry_scheduled
                else Outcome.dispose("max_turns_exceeded")
            )
        elif session.status == "rate_limit_circuit_open":
            # The AgentRunner's 429 backoff circuit breaker tripped
            # after ``rate_limit_max_retries`` consecutive rate
            # limit hits. Surface it on the dashboard and hand it
            # off to the inter-run retry queue with the longest
            # configured base delay so the provider's rate window
            # has a chance to reset before the next attempt.
            backoff_s = self._host.workflow.agent.rate_limit_max_backoff_ms
            logger.warning(
                "Rate limit circuit open issue_id=%s — scheduling "
                "inter-run retry with base delay %dms (session "
                "spent %.1fs in in-turn backoff across %d hits)",
                session.issue.id or "",
                backoff_s,
                getattr(session, "total_429_backoff_seconds", 0.0),
                getattr(session, "consecutive_429_count", 0),
            )
            self._host.status_dashboard.on_session_failed(
                session.issue.id or "",
                "rate_limit_circuit_open",
            )
            self._host._emit_im_event(
                session.issue.id or "",
                "agent.rate_limit_circuit_open",
                EventLevel.ERROR,
                "rate limit circuit open",
            )
            self._host._registry.mark_failed(session.issue.id or "")
            retry_scheduled = await self._host._schedule_retry(
                session,
                delay_base_ms=backoff_s,
            )
            if not retry_scheduled:
                await self._interpretation._sync_tracker_issue_state(session.issue.id or "", "failed")
            outcome = (
                Outcome.retry(reason="rate_limit_circuit_open")
                if retry_scheduled
                else Outcome.dispose("rate_limit_circuit_open")
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
            self._host.status_dashboard.on_session_failed(
                session.issue.id or "",
                str(session.status),
            )
            self._host._emit_im_event(
                session.issue.id or "",
                f"agent.{session.status}",
                EventLevel.WARN,
                getattr(session, "session_end_summary", "") or str(session.status),
            )
            self._host._registry.mark_failed(session.issue.id or "")
            await self._interpretation._sync_tracker_issue_state(session.issue.id or "", "failed")
            # No retry — same agent will likely repeat the
            # same loop on retry without human intervention.
            # The cron tick will mark the issue abandoned on
            # the next pass and the operator can either
            # adjust the issue / workflow or skip it.
            outcome = Outcome.dispose(str(session.status))
        elif session.status == "cancelled":
            logger.info(
                "Issue %s cancelled by operator — skipping retry",
                session.issue.id,
            )
            self._host.status_dashboard.on_session_failed(
                session.issue.id or "",
                "cancelled",
            )
            self._host._emit_im_event(
                session.issue.id or "",
                "issue.cancelled",
                EventLevel.WARN,
                "cancelled by operator",
            )
            self._host._registry.mark_failed(session.issue.id or "")
            await self._interpretation._sync_tracker_issue_state(session.issue.id or "", "failed")
            # Do NOT schedule retry — operator explicitly cancelled.
            outcome = Outcome.dispose("cancelled")
        elif session.status == "released":
            # Claim released by daemon shutdown (handled in the
            # CancelledError branch above): the registry record is
            # back to PENDING and the tracker re-opened. Falling
            # through to the generic-failure branch would clobber
            # mark_pending with mark_failed, re-sync the tracker to
            # 'failed', emit a spurious issue.failed event and
            # schedule a retry — i.e. reintroduce the exact bug the
            # release path exists to fix.
            logger.info(
                "Issue %s released by shutdown — left PENDING for re-dispatch",
                session.issue.id,
            )
            outcome = Outcome.dispose("released")
        else:
            self._host.status_dashboard.on_session_failed(
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
            self._host._emit_im_event(
                session.issue.id or "",
                "issue.failed",
                EventLevel.WARN,
                detail,
                session_payload(
                    self._host.tracker,
                    self._host._registry,
                    session,
                    turns=getattr(session, "turn_count", None),
                ),
            )
            failure_detail = getattr(session, "operator_failure_detail", None)
            if failure_detail:
                self._host._registry.mark_failed_with_reason(
                    session.issue.id or "",
                    str(failure_detail),
                )
                self._host._registry.update_report(
                    session.issue.id or "",
                    session_end_reason=getattr(session, "session_end_reason", None),
                    session_end_summary=getattr(session, "session_end_summary", ""),
                )
            else:
                self._host._registry.mark_failed(session.issue.id or "")
            # Persist the end reason / summary for EVERY failure
            # path, not only the operator_failure_detail one: a
            # bare mark_failed leaves session_end_reason unset in
            # the registry, which made fast-fail runs (e.g. backend
            # spawn errors) impossible to diagnose after the fact.
            self._host._registry.update_report(
                session.issue.id or "",
                session_end_reason=getattr(
                    session, "session_end_reason", None
                ),
                session_end_summary=getattr(
                    session, "session_end_summary", ""
                ),
            )
            # Gate the tracker close on the retry outcome: with a
            # retry pending the issue stays open+assigned on the
            # tracker; when the retry limit is reached the
            # ``_schedule_retry`` abandoned path closes it.
            retry_scheduled = await self._host._schedule_retry(session)
            if not retry_scheduled:
                await self._interpretation._sync_tracker_issue_state(session.issue.id or "", "failed")
            outcome = (
                Outcome.retry(reason=str(session.status))
                if retry_scheduled
                else Outcome.dispose(str(session.status))
            )

        # Update summary comment for non-completed paths (a
        # shutdown-released run posts no failure summary — the
        # issue is requeued, not failed).
        if (
            session.issue.id not in self._host._state.pending_review
            and session.status != "released"
        ):
            await self._interpretation._update_issue_summary(session)

        return outcome

    def control_commands(self) -> dict[str, CommandHandler]:
        """Return the application-owned operator command registry.

        The six issue-specific commands use one normalized
        ``(dedup_key, extra)`` handler signature. Kernel commands such as
        pause/resume/stop and gateway control remain mechanism-owned.
        """
        commands = self._commands

        async def _review_followup(issue_id: str, extra: str) -> bool:
            await commands._handle_review_followup_control(issue_id, extra)
            return True

        async def _rebase(issue_id: str, extra: str) -> bool:
            await commands._handle_rebase_control(issue_id, extra)
            return True

        async def _review_approve(issue_id: str, extra: str) -> bool:
            await commands._handle_review_approve_control(issue_id, extra)
            return True

        async def _review_retry(issue_id: str, extra: str) -> bool:
            await commands._handle_review_retry_control(issue_id, extra)
            return True

        async def _retry(issue_id: str, extra: str) -> bool:
            await commands._handle_retry_control(issue_id, extra)
            return True

        async def _followup(issue_id: str, extra: str) -> bool:
            return bool(await commands._handle_followup_control(issue_id, extra))

        return {
            "review_followup": _review_followup,
            "rebase": _rebase,
            "review_approve": _review_approve,
            "review_retry": _review_retry,
            "retry": _retry,
            "followup": _followup,
        }
