"""Issue→PR WorkProvider（DESIGN §4.1）——业务向机制层供数的接缝。

C2a 形态：每-poll 业务链（tracker 轮询、预登记、意图解析、角色资格、
限流、ack/registry 记账、BLOCKED/RETRY/FOLLOWUP/REBASE 内联处理、
terminal/PR 跳过、依赖检查、澄清门）已自宿主（Orchestrator）机械迁入
``_dispatch_candidates``——两侧 strip 归一 + ``self.``→``self._host.``
改写后逐行等值；launch 仍委托宿主 ``_launch_issue``。返回本轮已派发的
工作项。Kernel 侧的并发上限与 launch 拆分随 P4 余量切片迁入。

宿主经结构化协议引用；本模块不得在模块顶层 import orchestrator/
orchestration_subsystem/app——组合根 orchestrator.py 顶层 import 本包
（prompts 注册级联），反向依赖会成环（import 时序契约见
``applications/issue_pr/__init__.py`` 说明）。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Protocol

from orchestratord.events import EventLevel
from orchestratord.issue_registry import IssueStatus
from orchestratord.kernel.work_provider import WorkItem
from orchestratord.tracker import Command, Intent
from .payloads import issue_payload

if TYPE_CHECKING:
    from orchestratord.config.schema import WorkflowConfig
    from orchestratord.git.sync import PRRebaseResult
    from orchestratord.issue_clarifier.gate import IssueClarificationGate
    from orchestratord.issue_registry import IssueRegistry
    from orchestratord.issue_registry.issue import Issue
    from orchestratord.kernel.dispatch import OrchestratorState
    from orchestratord.tracker import CommandIntent, TrackerAdapter

logger = logging.getLogger(__name__)


class _IssueDispatchHost(Protocol):
    """宿主暴露给 WorkProvider 的机制组合接缝。

    Issue intent/rebase/feedback operations are resolved by the application
    facade through the composition root's finite compatibility lookup; they
    are deliberately absent from this protocol.
    """

    tracker: TrackerAdapter
    workflow: WorkflowConfig
    _registry: IssueRegistry
    _state: OrchestratorState
    _clarification_gate: IssueClarificationGate | None

    def _emit_im_event(
        self,
        issue_id: str,
        event_type: str,
        level: EventLevel,
        message: str = "",
        payload: dict[str, Any] | None = None,
    ) -> None: ...

    async def _launch_issue(self, issue: Issue) -> None: ...


class IssuePrWorkProvider:
    """kernel ``WorkProvider`` 协议的 issue→PR 实现（结构化满足）。"""

    def __init__(
        self,
        host: _IssueDispatchHost | None = None,
        application: Any | None = None,
    ) -> None:
        # C2c 两段式装配（DESIGN §4.2/G2）：组合根可先以无宿主形态构造
        # （宿主彼时尚未存在），由 Orchestrator.__init__ 回绑 ``_host``。
        self._host = host
        self._application = application

    def bind_application(self, application: Any) -> None:
        """Bind the business facade after the composition root is complete."""
        self._application = application

    def _business(self) -> Any:
        application = self._application
        if application is None and self._host is not None:
            application = getattr(self._host, "_issue_app", None)
        return getattr(application, "_interpretation", None) or application or self._host

    async def poll(self) -> list[WorkItem]:
        """拉取并派发本轮可执行的 issue 工作项（业务链见 ``_dispatch_candidates``）。"""
        return await self._dispatch_candidates()

    async def on_dispatch_rejected(self, item: WorkItem, reason: str) -> None:
        """工作项被限流/取消时的处置（P4 余量接入；现仅记录）。"""
        logger.warning(
            "issue work item %s rejected by dispatcher: %s",
            item.dedup_key,
            reason,
        )

    async def _dispatch_candidates(self) -> list[WorkItem]:
        """Issue→PR 每-poll 业务链（P4 C2a 自宿主 ``_dispatch_candidates``
        机械迁入；DESIGN §4.1）：tracker 拉取候选、预登记、意图解析与各
        业务门、launch 与槽位记账。返回本轮已派发（进入 running）的工作项。"""
        # Fetch new candidate issues
        try:
            issues = await self._host.tracker.fetch_candidate_issues()
        except Exception as exc:
            logger.error("Failed to fetch candidate issues: %s", exc)
            return []

        available_slots = self._host._state.max_concurrent_agents - len(self._host._state.running)
        if self._host._clarification_gate is not None:
            self._host._clarification_gate.begin_poll()

        # Pre-register all unregistered candidates with QUEUED status
        # so the dashboard / registry reflects the full backlog.
        for issue in issues:
            if not self._host._registry.get(issue.id or ""):
                base_branch = (
                    getattr(issue, "base_branch", None)
                    or self._host.workflow.workspace.base_branch
                    or "main"
                )
                self._host._registry.register(
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
                self._host._emit_im_event(
                    issue.id or "",
                    "issue.detected",
                    EventLevel.INFO,
                    "新增 ISSUE",
                    issue_payload(self._host.tracker, issue, url=issue.url),
                )
            elif issue.author_login:
                record = self._host._registry.get(issue.id or "")
                if record is not None and not record.author_login:
                    record.author_login = issue.author_login
                    self._host._registry._save()

        if self._host.workflow.workspace.strategy == "sequential" and self._host._state.running:
            return []

        launched_this_poll = 0
        launched_items: list[WorkItem] = []
        for issue in issues:
            if launched_this_poll >= available_slots:
                break
            if issue.id in self._host._state.running:
                continue
            if issue.id in self._host._state.claimed:
                continue

            # Intent resolution must
            # happen BEFORE the completed/pending_review skip so
            # operators can trigger RETRY / FOLLOWUP on completed
            # issues via labels, comments, or CLI.
            intent, command_intent_obj, intent_source = await self._business()._resolve_intent(issue)

            if issue.id in self._host._state.completed or issue.id in self._host._state.pending_review:
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
                and not self._business()._is_command_author_eligible(issue, command_author)
            ):
                await self._business()._reject_unauthorized_command(issue, command_intent_obj)
                continue

            # Rate limit on RETRY intent. If the issue
            # has hit `max_retries_per_issue`, refuse the reset
            # (even with `--force`; only the label-based retry
            # honors force in the daemon path).
            if intent is Intent.RETRY:
                if not self._business()._check_retry_rate_limit(issue, force=False):
                    continue

            # When a comment command is honored, post
            # a bot acknowledgement so the operator sees the
            # intent was received, and record the command on the
            # registry for audit.
            if command is not None:
                await self._business()._post_command_acknowledgement(issue, command)
                record = self._host._registry.get(issue.id or "")
                if record is not None:
                    record.last_command = f"/agent {command.value}"
                    record.touch()
                    self._host._registry._save()
                logger.info(
                    "Issue %s command received: /agent %s",
                    issue.id,
                    command.value,
                )

                # UNBLOCK is a meta-command: clear any BLOCKED
                # state so the next poll re-applies the (now
                # possibly cleared) label-based intent.
                if command is Command.UNBLOCK:
                    record = self._host._registry.get(issue.id or "")
                    if record is not None and record.status is IssueStatus.ABANDONED:
                        logger.info(
                            "Issue %s unblocked, status reset to pending",
                            issue.id,
                        )
                        record.status = IssueStatus.PENDING
                        record.intent = Intent.NONE
                        record.intent_source = None
                        self._host._registry._save()

            if intent is Intent.BLOCKED:
                logger.info(
                    "Issue %s blocked intent detected, marking abandoned",
                    issue.id,
                )
                record = self._host._registry.get(issue.id or "")
                if record is None:
                    self._host._registry.register(
                        issue_id=issue.id or "",
                        issue_identifier=issue.identifier or "",
                        branch_name=getattr(issue, "branch_name", None) or "main",
                    )
                self._host._registry.mark_intent(
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
                self._host._registry.mark_abandoned(issue.id or "")
                await self._business()._sync_tracker_issue_state(issue.id or "", "abandoned")
                self._host._state.completed.add(issue.id or "")
                continue

            if intent is Intent.RETRY:
                logger.info(
                    "Issue %s retry intent detected, will reset on launch",
                    issue.id,
                )
                self._host._registry.mark_intent(
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
                self._host._registry.mark_intent(
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
                followup_record = self._host._registry.get(issue.id or "")
                if self._business()._uses_review_feedback_followup(followup_record):
                    # Command follow-up handles pending PR review feedback
                    # instead of rerunning the entire issue. Dashboard chat
                    # keeps its agent_followup path because the operator's
                    # text is the work to perform.
                    followup_handled = (
                        await self._business()._launch_followup_with_pending_reviews(issue)
                    )
                    if followup_handled:
                        continue
                    # Do not rerun the issue when no feedback is pending.
                    logger.info(
                        "Issue %s follow-up: no pending review feedback "
                        "to process — skip",
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
                self._host._registry.mark_intent(
                    issue.id or "",
                    intent,
                    source=(intent_source or ("command" if command is not None else "label")),
                    command=(f"/agent {command.value}" if command is not None else None),
                )
                if not self._business()._check_rebase_rate_limit(issue, force=False):
                    continue
                await self._business()._process_rebase_intent(issue)
                # CLI is one-shot; clear so the next poll doesn't
                # re-trigger. Audit + last_command are preserved.
                if intent_source == "cli":
                    self._host._registry.clear_intent(issue.id or "")
                continue

            # Skip terminal registry records even if the tracker still
            # exposes the issue in an active state. Explicit retry/follow-up
            # intents are the only daemon path that may reopen handled work.
            if intent is Intent.NONE and (
                self._host._registry.is_terminal(issue.id or "")
                or self._host._registry.has_pr(issue.id or "")
            ):
                logger.info("Issue %s already handled (registry), skipping", issue.id)
                continue
            if not await self._business()._dependencies_satisfied(issue):
                continue
            if self._host._clarification_gate is not None:
                try:
                    if not await self._host._clarification_gate.should_dispatch(issue):
                        logger.info("Issue %s is waiting for issue-clarifier clarification", issue.id)
                        continue
                except Exception:
                    logger.exception("Issue-clarifier clarity gate failed for issue %s", issue.id)
                    if not bool(getattr(self._host.workflow.clarifier, "fail_open", True)):
                        continue
            self._host._state.claimed.add(issue.id)
            # Thread-local MDC for the orchestrator launch path —
            # the agent_runner will refill with run_id once available.
            from orchestratord.logging_setup import set_log_context

            set_log_context(
                issue_id=str(issue.id or ""),
                issue_identifier=str(getattr(issue, "identifier", "")),
            )
            await self._host._launch_issue(issue)
            if issue.id in self._host._state.running:
                launched_this_poll += 1
                launched_items.append(
                    WorkItem(dedup_key=str(issue.id or ""), business={"issue": issue})
                )
                # CLI retry is a one-shot. The
                # operator's orchestrator issue command
                # retry --mode reset` already wrote `registry.intent`
                # with `intent_source="cli"`; now that the launch
                # has started, clear it so the next poll does NOT
                # re-trigger. The audit trail (the original
                # `last_command` text + the high-priority audit
                # log entry written by the CLI) is preserved.
                if intent_source == "cli":
                    self._host._registry.clear_intent(issue.id or "")

        return launched_items
