"""Issue→PR operator command handlers owned by the application boundary."""

from __future__ import annotations

import logging
from typing import Any

from orchestratord.events import EventLevel
from orchestratord.issue_registry import IssueStatus
from orchestratord.tracker import Intent

logger = logging.getLogger(__name__)


class IssuePrCommands:
    """Issue→PR operator command handlers owned by the application boundary."""

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
        return getattr(self.host, name)

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

    async def _handle_followup_control(self, issue_id: str, extra: str) -> bool:
        """Re-launch a completed issue with FOLLOWUP intent.

        Unified handler for both CLI ``--mode followup`` and chat follow-up.
        Unlike ``_handle_retry_control`` this does NOT reset the PR or
        branch — the agent reuses the existing branch and appends a
        follow-up commit.  The follow-up prompt text is read from
        ``.operator_hints.md`` by ``prompt_builder`` at launch time.
        """
        if not issue_id:
            return True
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
            return True

        if issue_id in self._state.running:
            logger.debug(
                "Deferring chat follow-up for active issue %s until its run exits",
                issue_id,
            )
            return False

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
                r for r in retry_queue if self._retry_dedup_key(r) != issue_id
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
        return True

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

__all__ = ["IssuePrCommands"]
