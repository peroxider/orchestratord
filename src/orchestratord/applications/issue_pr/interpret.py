"""Issue→PR business interpretation owned by the application boundary.

The composition root supplies a host object for infrastructure and mechanism
services. Business decisions and tracker/registry side effects live here; the
kernel only schedules the resulting lifecycle.
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Any

from orchestratord.events import EventLevel
from orchestratord.failure_messages import end_reason_guidance
from orchestratord.git.sync import PRRebaseResult, rebase_for_pr
from orchestratord.git.utils import get_repo_root, run_git as _run_git
from orchestratord.issue_registry import IssueStatus
from orchestratord.issue_registry.issue import Issue
from orchestratord.review_feedback import REPLY_MARKER, ReviewFeedbackService, ReviewFollowup
from orchestratord.session_state import AgentSession
from orchestratord.status_dashboard import SessionStatus
from orchestratord.tracker import (
    CommandIntentCapability,
    Intent,
    MergeableStatus,
    PullRequestFeedback,
    PullRequestFeedbackCapability,
    PullRequestMaintenanceCapability,
    PullRequestRef,
    command_to_intent,
    merge_intents_with_cli,
    supports,
)
from .prompts import render_feedback_summary, render_rebase, render_review_feedback
from .payloads import issue_payload

logger = logging.getLogger(__name__)
_SUCCESS_END_REASONS = frozenset({"success"})


class IssuePrInterpretation:
    """Issue→PR intent, rebase, review, and finalization operations."""

    def __init__(self, host: Any) -> None:
        self.host = host

    def __getattribute__(self, name: str) -> Any:
        if name not in {"host", "__dict__", "__class__"}:
            try:
                host = object.__getattribute__(self, "host")
                override = vars(host).get(name)
            except (AttributeError, TypeError):
                override = None
            if override is not None:
                return override
        return object.__getattribute__(self, name)

    def __getattr__(self, name: str) -> Any:
        """Resolve infrastructure/machine services from the composition root."""
        return getattr(self.host, name)

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
            loop = asyncio.get_running_loop()
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
            getattr(self.host, "_rebase_for_pr", rebase_for_pr),
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
        invokes ``_launch_rebase_resolution`` as a background task so the
        agent_rebase run does not block the poll loop (control commands,
        retry queue, heartbeat, candidate issue fetch). Each resolution
        opens a fresh ``AgentSession`` whose prompt is built by
        ``PromptBuilder.render_rebase``. The done-callback calls
        ``_finalize_rebase_resolution`` for post-run state migration.
        """
        available_slots = self._state.max_concurrent_agents - len(self._state.running)
        if available_slots <= 0:
            logger.debug("No concurrency slots for rebase-resolution")
            return

        # Background tasks only populate ``_state.running`` once they start
        # executing, so the top-of-function slot check alone would let this
        # loop oversubscribe beyond ``max_concurrent_agents`` when several
        # records carry conflicts at once. Mirror ``_poll_and_dispatch`` and
        # stop launching once this poll has consumed the available slots.
        launched_this_poll = 0
        records_snapshot = list(self._registry._records.values())
        for record in records_snapshot:
            if launched_this_poll >= available_slots:
                break
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
            # Launch as a background task so a long agent_rebase run does not
            # block the poll loop (control commands / retry queue / heartbeat).
            launched_this_poll += 1
            task = asyncio.create_task(self._launch_rebase_resolution(issue_obj))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

            # Done-callback: run _finalize_rebase_resolution after the
            # agent_rebase run completes, so the state migration (conflict
            # clearing, IM events, audit) does not block the poll loop.
            def _finalize_rebase_callback(t: asyncio.Task, issue=issue_obj) -> None:
                try:
                    session = t.result()
                except Exception as exc:
                    logger.error(
                        "Issue %s rebase-resolution task raised: %s", issue.id, exc
                    )
                    return
                if session is None:
                    return
                finalize_task = asyncio.create_task(
                    self._finalize_rebase_resolution(issue, session)
                )
                self._tasks.add(finalize_task)

                # Log exceptions from the finalizer so they don't
                # become unhandled task exceptions (mirrors the
                # try/except that wrapped the original inline call).
                def _finalize_done(t2: asyncio.Task) -> None:
                    self._tasks.discard(t2)
                    exc = t2.exception()
                    if exc is not None:
                        logger.error(
                            "Issue %s rebase-resolution finalizer raised: %s",
                            issue.id,
                            exc,
                        )

                finalize_task.add_done_callback(_finalize_done)

            task.add_done_callback(_finalize_rebase_callback)

    def _extract_pr_state(self, status: MergeableStatus) -> str:
        """Extract the remote PR ``state`` from a mergeable fetch payload.

        Returns ``""`` when the payload does not expose a state (e.g. the
        GitCode JS-rendered fallback) so callers can treat it as "unknown"
        and keep scanning. ``closed`` PRs with ``merged: true`` are
        normalized to ``"merged"`` so the registry records the semantic
        state instead of the raw GitHub value.
        """
        raw = getattr(status, "raw", None)
        payload = raw.get("payload", {}) if isinstance(raw, dict) else {}
        if not isinstance(payload, dict):
            return ""
        state = str(payload.get("state") or "").strip().lower()
        if state == "closed" and payload.get("merged"):
            return "merged"
        return state

    def _is_terminal_issue_state(self, state: str | None) -> bool:
        """True when the tracker reports the issue in a terminal state.

        Uses the tracker adapter's ``terminal_states`` vocabulary when
        available (list of strings, e.g. ``["closed"]`` for GitHub /
        ``["Done", "Cancelled", ...]`` for Linear); returns False for
        unknown / unset states so the daemon never blocks a legitimate
        rebase on a missing vocabulary.
        """
        if not state or not state.strip():
            return False
        terminal_states = getattr(self.tracker, "terminal_states", None)
        if not isinstance(terminal_states, (list, tuple)):
            return False
        normalized = {s.strip().lower() for s in terminal_states if s.strip()}
        return state.strip().lower() in normalized

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
            if status is None:
                continue
            # 从 PR 详情载荷中取真实状态（open/closed/merged）并写回 registry：
            # 非 open（如已合并/已关闭）的 PR 后续周期零成本跳过，不再反复
            # 探测 mergeable、烧限流配额。仅当载荷带 state 时才写回，避免
            # 覆盖已有记录。
            remote_pr_state = self._extract_pr_state(status)
            if remote_pr_state:
                if record.pr_state != remote_pr_state:
                    record.pr_state = remote_pr_state
                    self._registry._save()
                if remote_pr_state not in cfg.scan_states:
                    continue
            if not status.has_conflicts:
                continue
            issue = await self.tracker.fetch_issue_states_by_ids([issue_id])
            issue_obj = issue.get(issue_id) if issue else None
            # 防竞态：rebase intent 触发前再校验一次 issue 的 tracker 状态
            # 非终态（扫描期间 issue 可能已被关闭/完成）。
            if issue_obj is not None and self._is_terminal_issue_state(issue_obj.state):
                logger.info(
                    "Issue %s in terminal tracker state %r, skipping rebase intent",
                    issue_id,
                    issue_obj.state,
                )
                continue
            if issue_obj is None:
                issue_obj = Issue(
                    id=issue_id,
                    identifier=record.issue_identifier,
                    title="(unknown)",
                    branch_name=record.branch_name,
                )
            await self._process_rebase_intent(issue_obj)

    async def _launch_rebase_resolution(self, issue: Issue) -> AgentSession:
        """Launch an ``agent_rebase`` session to resolve a content conflict.

        Mirrors ``_launch_issue`` for the conflict-resolution path.
        The session is tagged with ``run_kind="agent_rebase"`` so the
        agent runner can route the run through a rebase-tailored
        prompt and dispatch policy.

        The caller (``_process_pending_rebase_conflicts``) invokes this
        via ``asyncio.create_task`` and attaches a done-callback that
        calls ``_finalize_rebase_resolution`` — so the agent run does
        not block the poll loop.

        Returns the ``AgentSession`` so the done-callback can pass it
        to ``_finalize_rebase_resolution``.
        """
        record = self._registry.get(issue.id or "")
        workspace_path = record.workspace_path if record else None
        # Synthesize a minimal Workspace stub when no real workspace
        # is available; the agent runner only needs ``workspace.path``
        # to be present for prompt injection.
        if workspace_path:
            from pathlib import Path as _Path

            from orchestratord.workspace import Workspace as _Ws

            workspace = _Ws(path=_Path(workspace_path), issue_identifier=issue.identifier or "")
        else:
            from pathlib import Path as _Path

            from orchestratord.workspace import Workspace as _Ws

            workspace = _Ws(path=_Path("/tmp"), issue_identifier=issue.identifier or "")
        session = AgentSession(
            subject=issue,
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
        session.prompt_override = render_rebase(
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
        # The caller (``_process_pending_rebase_conflicts``) runs this via
        # ``asyncio.create_task`` and completes the state migration from its
        # done-callback (``_finalize_rebase_resolution``), so the agent run
        # never blocks the poll loop.
        return session

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
                issue_payload(self.host.tracker, issue, pr=pr_url, commit=new_head),
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
            issue_payload(self.host.tracker, issue, pr=pr_url),
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

    @staticmethod
    def _uses_review_feedback_followup(record: Any) -> bool:
        """Keep command follow-ups distinct from Dashboard conversation turns."""
        return bool(
            record is not None
            and record.intent is Intent.FOLLOWUP
            and record.intent_source != "chat"
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
                        pull_request=pull_request,
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
        prompt = render_review_feedback(
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
            subject=issue,
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

    async def _sync_tracker_issue_state(self, issue_id: str, state: str) -> bool:
        # Preserve an explicitly injected host seam used by lightweight
        # adapters/tests; normal composition has no such attribute and uses
        # this application-owned implementation.
        host_override = vars(self.host).get("_sync_tracker_issue_state")
        if host_override is not None:
            result = host_override(issue_id, state)
            if hasattr(result, "__await__"):
                return bool(await result)
            return bool(result)
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
        # Raw backend-side error (code + message), preserved separately
        # from session_end_summary which downstream guards overwrite.
        backend_error = getattr(session, "backend_error_detail", None)
        if backend_error and backend_error not in (reason_text, hook_error):
            body_lines.append(f"- Backend error: `{str(backend_error)[:300]}`")
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
        from orchestratord.review_feedback import REPLY_MARKER

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

        summary = render_feedback_summary(
            attempt=attempt,
            processed=processed,
            skipped=skipped,
        )
        try:
            await self.tracker.create_comment(session.issue.id or "", summary)
        except Exception as exc:
            logger.warning("Failed to post feedback summary issue_id=%s: %s", session.issue.id, exc)

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

__all__ = ["IssuePrInterpretation"]
