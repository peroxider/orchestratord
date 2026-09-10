"""Issue��PR execution lifecycle owned by the application layer."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Any

from orchestratord.debug_log import append_debug_event
from orchestratord.events import EventLevel
from orchestratord.failure_messages import *  # noqa: F403
from orchestratord.git.sync import (
    GitSyncPostCommitError,
    HookFailedError,
    VerificationFailed,
)
from orchestratord.git.utils import get_file_status, get_repo_root, run_git as _run_git
from orchestratord.issue_registry.task_mapping import issue_to_agent_task
from orchestratord.premise_check import format_cannot_proceed_comment, read_cannot_proceed
from orchestratord.runner_utils import _await_with_active_timeout
from orchestratord.session_state import AgentSession
from orchestratord.kernel.work_provider import WorkItem
from .repro import repro_gate_applies, run_repro_gate
from .payloads import session_payload
from .workflow import run_issue_with_workflow

logger = logging.getLogger(__name__)


def _operator_failure_detail(exc: BaseException) -> str:
    return " ".join(str(exc).split()) or exc.__class__.__name__


async def run_issue_body(host: Any, session: AgentSession) -> None:
    """Run agent for one issue with concurrency control."""
    async with host._semaphore:
        ran_agent = False
        workspace_dirty: bool | None = None
        try:
            await host.workspace.run_before_run_hook(
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
                max_turns=host.workflow.agent.max_turns,
                timeout_seconds=host.workflow.agent.run_timeout_ms / 1000.0,
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
                progress_sink = host._build_session_sink(session.issue.id or "")

                # Repro-first gate: before any fix work, a dedicated
                # reproduction pass must demonstrate the described
                # failure (executable check, non-zero exit). A closed
                # gate fails the issue with a "cannot reproduce"
                # report instead of an unverifiable fix MR.
                if repro_gate_applies(host, session):
                    gate_open = await run_repro_gate(host, session, progress_sink)
                    if not gate_open:
                        return

                # ��������� workflow.yaml��ʹ������ʽ����������
                # review_followup ʹ��ר�� prompt��render_review_feedback����
                # ���� workflow.yaml ������ stage ���̣�����ѭ����
                if (
                    host._workflow_orchestrator is not None
                    and session.run_kind != "review_followup"
                ):
                    await run_issue_with_workflow(host, session, progress_sink)
                else:
                    # Collaboration-mode dispatch. For the
                    # default ``single`` mode (the only one
                    # registered in Phase 1) we keep the legacy
                    # ``stage_runners[run_kind] or agent_runner``
                    # lookup so 270+ existing tests pass byte-
                    # identically. For non-single modes registered
                    # in later phases, we dispatch to the
                    # ``ModeRunner`` from the registry instead, and
                    runner = host._resolve_session_runner(session)
                    run_timeout_seconds = host.workflow.agent.run_timeout_ms / 1000.0
                    session.timeout_deadline_at = time.time() + run_timeout_seconds
                    await _await_with_active_timeout(
                        runner.run(
                            session,
                            host.workflow,
                            status_dashboard=host.status_dashboard,
                            tracker=host.tracker,
                            comment_tracker=host.tracker,
                            clarification_resolver=host._clarification_resolver,
                            progress_reporter=progress_sink,
                            diagnostics_callback=host._update_run_diagnostics,
                            # Ӧ�ò��ṩ rebase ��ͻ�ļ�װ�Σ�DESIGN ��4.2
                            # prepare_run seam�������Ʋ಻�ٶ�ȡ session ҵ���ֶΡ�
                            conflict_files=session.conflict_files,
                        ),
                        session=session,
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
                    # through to git_sync �� which would either open an MR
                    # around a fabricated fix or an empty branch.
                    _cannot = read_cannot_proceed(getattr(session.workspace, "path", None))
                    if _cannot is not None:
                        _reason = str(_cannot.get("reason", "cannot_proceed"))
                        session.status = "failed"
                        session.session_end_reason = "premise_not_met"
                        session.session_end_summary = str(_cannot.get("details", ""))[:500]
                        logger.warning(
                            "Issue %s: agent declared cannot_proceed (%s) �� "
                            "marking FAILED without creating a PR",
                            session.issue.id,
                            _reason,
                        )
                        host._registry.mark_failed_with_reason(
                            session.issue.id or "",
                            f"premise_not_met ({_reason}): agent declared the issue "
                            "cannot honestly be completed; no PR created.",
                        )
                        try:
                            await host.tracker.create_comment(
                                session.issue.id or "",
                                format_cannot_proceed_comment(session.issue, _cannot),
                            )
                        except Exception:
                            logger.warning(
                                "Issue %s: failed to post cannot_proceed comment",
                                session.issue.id,
                                exc_info=True,
                            )
                        await host._sync_tracker_issue_state(session.issue.id or "", "failed")
                        host.status_dashboard.on_session_complete(session.issue.id or "")
                        host._state.completed.add(session.issue.id or "")
                        host._state.failed.add(session.issue.id or "")
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
                                _file_status = await asyncio.to_thread(
                                    get_file_status, _repo_root
                                )
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
                                await host._complete_read_only_chat_followup(session)
                                return
                            logger.warning(
                                "Session completed but workspace has no changes "
                                "issue_id=%s �� marking as failed",
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
                            host.tracker,
                            __import__(
                                "orchestratord.local_tracker.adapter",
                                fromlist=["LocalTrackerAdapter"],
                            ).LocalTrackerAdapter,
                        )
                        else "default"
                    )
                    sync_result = await host.git_sync.sync(session, mode=sync_mode)
                    # ���ţ�daemon ������ read-only loop /
                    # stagnation ����ֹ����ʱ��git_sync ���ᴴ�� PR��
                    # ���� session_end_reason �б�� empty_branch_no_commits��
                    # ��ʱ������ mark_synced����� SYNCED + �� PR����
                    # ������ mark_failed_with_reason���� issue ���� FAILED��
                    # ��C2b�������߼�ǨӦ�ò� interpret_sync_result����
                    if await host._issue_app.interpret_sync_result(session, sync_result):
                        return
                    if sync_result is not None:
                        host._registry.update_report(
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
                            host._registry.mark_feedback_processed(
                                session.issue.id or "",
                                list(getattr(session, "feedback_ids", [])),
                                commit_sha=sync_result.commit_sha,
                            )
                            await host._reply_to_processed_feedback(session)
                            await host._post_feedback_summary(session, sync_result)
                            await host._apply_review_rules(session, sync_result)
                        elif session.run_kind in ("agent_followup", "review_retry"):
                            # A follow-up keeps the
                            # existing pr_number / pr_url / status;
                            # only the followup_attempt_count and
                            # last_followup_commit_sha change.
                            host._registry.increment_followup_attempt(session.issue.id or "")
                            if sync_result.commit_sha:
                                record = host._registry.get(session.issue.id or "")
                                if record is not None:
                                    record.last_followup_commit_sha = sync_result.commit_sha
                                    host._registry._save()
                                if session.run_kind == "review_retry":
                                    # Keep rejected-review feedback
                                    # available across failed attempts,
                                    # but consume it once a follow-up
                                    # commit has synced so a future reset
                                    # cannot replay stale advice.
                                    host._clarification_queue.consume_feedback(
                                        session.issue.id or ""
                                    )
                            logger.info(
                                "Issue %s followup committed: %s on %s",
                                session.issue.id,
                                sync_result.commit_sha,
                                sync_result.branch_name,
                            )
                        else:
                            host._registry.mark_synced(
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
                            host._emit_im_event(
                                session.issue.id or "",
                                "pr.updated" if is_followup else "pr.opened",
                                EventLevel.INFO,
                                "PR updated" if is_followup else "PR opened",
                                session_payload(
                                    host.tracker,
                                    host._registry,
                                    session,
                                    pr=pr_url,
                                    commit=getattr(sync_result, "commit_sha", None),
                                ),
                            )
                        # Review gate: after commit, await human review before completion.
                        # Triggered when GitSyncResult.pending_review is True (LocalTracker
                        # by default, or any tracker when agent.review_required=True in workflow).
                        if sync_result.pending_review:
                            if host.workflow.agent.auto_approve:
                                logger.info(
                                    "Issue %s auto-approved (auto_approve=True) �� "
                                    "skipping pending_review gate",
                                    session.issue.id,
                                )
                            else:
                                host._registry.mark_pending_review(session.issue.id or "")
                                await host._sync_tracker_issue_state(
                                    session.issue.id or "", "pending_review"
                                )
                                host.status_dashboard.on_session_complete(
                                    session.issue.id or ""
                                )
                                host._emit_im_event(
                                    session.issue.id or "",
                                    "pr.pending_review_gate",
                                    EventLevel.WARN,
                                    "pending human review",
                                    session_payload(host.tracker, host._registry, session, pr=pr_url),
                                )
                                host._state.pending_review.add(session.issue.id or "")
                                # Do NOT cleanup workspace �� human needs to review it
                                return

                    # Downstream compatibility deviation (TODO upstream-merge):
                    # salvage override �� when the widened gate above let
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
                    # out of budget mid-work, so the��β steps (report
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
                            "%s �� overriding status to completed and "
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
                        # Persist the salvage reason now: the earlier
                        # update_report already wrote the pre-salvage
                        # failure reason, and the completed branch of
                        # the terminal chain only calls mark_completed.
                        # `orchestratord issue review --reject` detects
                        # salvageable completions via this end reason.
                        host._registry.update_report(
                            session.issue.id or "",
                            session_end_reason=session.session_end_reason,
                            session_end_summary=session.session_end_summary,
                        )
            finally:
                await host.workspace.run_after_run_hook(
                    session.workspace,
                    session.issue,
                )
        except GitSyncPostCommitError as exc:
            sync_result = exc.result
            host._registry.update_report(
                session.issue.id or "",
                report_path=getattr(session, "report_path", None),
                verification_status=getattr(session, "verification_status", None),
                verification_output=getattr(session, "verification_output", None),
                summary_comment_id=getattr(session, "summary_comment_id", None),
                session_end_reason=getattr(session, "session_end_reason", None),
                session_end_summary=getattr(session, "session_end_summary", ""),
            )
            if session.run_kind in ("agent_followup", "review_retry"):
                record = host._registry.get(session.issue.id or "")
                if record is not None and sync_result.commit_sha:
                    record.last_followup_commit_sha = sync_result.commit_sha
                    host._registry._save()
            elif session.run_kind != "review_followup":
                host._registry.mark_synced(
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
            host._emit_im_event(
                session.issue.id or "",
                "post_commit_failed",
                EventLevel.ERROR,
                str(exc),
                session_payload(
                    host.tracker,
                    host._registry,
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
                # ���Ӵ���ʧ�ܣ������ʧ�ܼ��� +1���ﵽ��ֵ���������
                # ���ٷ����������� token�����ջظ�˵������ԭ�򣩡�
                host._registry.increment_feedback_failure(
                    session.issue.id or "",
                    list(getattr(session, "feedback_ids", [])),
                )
            host._emit_im_event(
                session.issue.id or "",
                "verification.failed",
                EventLevel.WARN,
                exc.output or str(exc),
                session_payload(host.tracker, host._registry, session),
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
            host._emit_im_event(
                session.issue.id or "",
                "verification.failed",
                EventLevel.WARN,
                f"{exc.hook_name}: {exc.output or exc}",
                session_payload(host.tracker, host._registry, session),
            )
        except asyncio.TimeoutError:
            reason = (
                "Agent run exceeded configured timeout "
                f"({host.workflow.agent.run_timeout_ms}ms)"
            )
            logger.warning(
                "Agent run timed out issue_id=%s timeout_ms=%s",
                session.issue.id,
                host.workflow.agent.run_timeout_ms,
            )
            workspace_dirty = bool(
                await asyncio.to_thread(
                    get_file_status, str(session.workspace.path)
                )
            )
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
                timeout_ms=host.workflow.agent.run_timeout_ms,
            )
            session.status = "agent_timeout"
            session.verification_status = "failed"
            session.verification_output = reason
            host._emit_im_event(
                session.issue.id or "",
                "issue.failed",
                EventLevel.WARN,
                reason,
                session_payload(
                    host.tracker,
                    host._registry,
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
            if host._shutdown_event.is_set():
                # Daemon shutdown (SIGTERM teardown cancels pending
                # tasks via asyncio.run) interrupted the run. The
                # issue did not fail on its own merits �� release the
                # claim so the next daemon start re-dispatches it,
                # instead of leaving a FAILED record that permanently
                # blocks dispatch (terminal registry entries are
                # skipped by _poll_and_dispatch).
                logger.warning(
                    "Agent run interrupted by daemon shutdown "
                    "issue_id=%s �� releasing claim for re-dispatch",
                    session.issue.id,
                )
                session.status = "released"
                session.session_end_reason = "shutdown_released"
                session.session_end_summary = (
                    "daemon shutdown interrupted the run; issue requeued"
                )
                session.verification_status = None
                session.verification_output = None
                host._registry.mark_pending(session.issue.id or "")
                try:
                    await host._sync_tracker_issue_state(
                        session.issue.id or "", "open"
                    )
                except Exception:
                    logger.debug(
                        "tracker release sync failed issue_id=%s",
                        session.issue.id,
                        exc_info=True,
                    )
                return
            logger.warning(
                "Agent run cancelled issue_id=%s �� cleaning up workspace",
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
                issue_record = host._registry.get(session.issue.id)
                await host.workspace.cleanup(
                    session.issue,
                    end_status=session.status,
                    end_reason=session.session_end_reason,
                    agent_config=getattr(host, "_agent_config", None),
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
            host._update_run_diagnostics(session)
            # Diagnostics saves are throttled; force the final
            # snapshot to disk in case this path (e.g. pending_review)
            # ends without a durable status mutation.
            host._registry.flush()

            if session.issue.id in host._state.running:
                del host._state.running[session.issue.id]

            # Push today's telemetry summary after the run ends
            # (best-effort; no-op unless workflow.telemetry is enabled).
            host._report_telemetry()
            # Dashboard journal: one terminal event per run with the
            # final status plus the session/PR references the issue
            # accumulated. Best-effort �� never raises.
            if host._viz_journal is not None:
                try:
                    _iid = str(session.issue.id or "")
                    _rec = host._registry.get(_iid)
                    if getattr(session, "run_id", None):
                        host._viz_journal.write_event(
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
                        host._viz_journal.write_event(
                            {
                                "type": "pr_status",
                                "issue_id": _iid,
                                "pr_url": _rec.pr_url,
                                "pr_number": _rec.pr_number,
                            }
                        )
                    _status = str(session.status or "")
                    if _status == "completed":
                        host._viz_journal.write_event(
                            {
                                "type": "complete",
                                "issue_id": _iid,
                                "overall_status": "completed",
                            }
                        )
                    elif _status and _status != "released":
                        # A shutdown-released run is not an error �� the
                        # issue was requeued, so no journal error event.
                        host._viz_journal.write_event(
                            {
                                "type": "error",
                                "issue_id": _iid,
                                "error": getattr(session, "session_end_summary", "") or _status,
                            }
                        )
                except Exception:
                    logger.debug("viz journal final event failed", exc_info=True)

            # ҵ����̬ӳ������ǨӦ�òࣨC2b��DESIGN ��4.2 interpret
            # seam����registry ״̬����tracker ͬ����IM ֪ͨ��run
            # summary comment ��Ӧ�ý��Ͳ����� Outcome��Kernel ��
            # Outcome ������ dispatch-loop ��Ƭ���롣workspace ����
            # ����ȫ���ԣ��� claimed �ͷ�Ϊ���ƶΣ������� finally��
                outcome = await host._issue_app.interpret(session)
                kernel = getattr(host, "_kernel", None)
                if kernel is not None:
                    await kernel.consume_outcome(
                        WorkItem(
                            dedup_key=str(session.issue.id or ""),
                            task=session.task,
                            business={"session": session},
                        ),
                        outcome,
                    )

            # Cleanup workspace based on preservation policy
            try:
                issue_record = host._registry.get(session.issue.id)
                await host.workspace.cleanup(
                    session.issue,
                    end_status=getattr(session, "status", None),
                    end_reason=getattr(session, "session_end_reason", None),
                    agent_config=getattr(host, "_agent_config", None),
                    issue_record=issue_record,
                )
            except Exception as exc:
                logger.warning(
                    "Workspace cleanup failed issue_id=%s: %s",
                    session.issue.id,
                    exc,
                )

            host._state.claimed.discard(session.issue.id or "")
