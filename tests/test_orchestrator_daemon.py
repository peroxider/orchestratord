"""Step 7: orchestrator daemon REBASE 分支 + agent reentry.

Covers:
  - _check_rebase_rate_limit:
      * under limit → True + incremented count
      * at limit + force=False → False + audit rebase_rejected
      * at limit + force=True → True + audit not rebase_rejected
  - _process_rebase_intent:
      * clean rebase → clear_conflict + commit_sha update
      * conflict rebase → mark_conflict with conflict_files
      * missing record → None (skipped)
      * missing workspace/branch → None (skipped)
  - _process_pending_rebase_conflicts:
      * empty when no record has has_conflict
      * launches agent_rebase when record has has_conflict
      * rate-limited records are skipped
  - _process_pr_conflict_scan:
      * disabled by default (no-op)
      * enabled but tracker returns no PRs → no-op
      * enabled + tracker reports has_conflicts → invokes _process_rebase_intent
      * GitCode fallback (status=None) is silently skipped
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from orchestratord.agent_runner import AgentSession
from orchestratord.applications.issue_pr.interpret import IssuePrInterpretation
from orchestratord.applications.issue_pr.lifecycle import IssueToPrLifecycle
from orchestratord.config.schema import (
    PrConflictScanConfig,
    WorkflowConfig,
)
from orchestratord.git import GitSyncService, get_file_status
from orchestratord.git.sync import PRRebaseResult
from orchestratord.issue_registry import (
    IssueRegistry,
    IssueStatus,
)
from orchestratord.issue_registry.issue import Issue
from orchestratord.orchestrator import Orchestrator
from orchestratord.tracker import Intent, MergeableStatus, PullRequestRef


class TestReadOnlyChatFollowup(unittest.IsolatedAsyncioTestCase):
    def test_chat_followup_does_not_require_pending_pr_feedback(self) -> None:
        record = SimpleNamespace(intent=Intent.FOLLOWUP, intent_source="chat")

        self.assertFalse(IssuePrInterpretation._uses_review_feedback_followup(record))

    def test_command_followup_uses_pending_pr_feedback(self) -> None:
        record = SimpleNamespace(intent=Intent.FOLLOWUP, intent_source="cli")

        self.assertTrue(IssuePrInterpretation._uses_review_feedback_followup(record))

    async def test_completed_read_only_followup_returns_to_review(self) -> None:
        """A conversational follow-up is valid even without a new commit."""
        orch = Orchestrator.__new__(Orchestrator)
        orch._registry = MagicMock()
        orch._state = SimpleNamespace(pending_review=set())
        orch.status_dashboard = MagicMock()
        orch._sync_tracker_issue_state = AsyncMock()
        orch._emit_im_event = MagicMock()
        session = SimpleNamespace(
            issue=SimpleNamespace(id="7"),
            run_id="run-followup-7",
            report_path="/tmp/run-followup-7.json",
            verification_status="passed",
            verification_output="7 passed",
            summary_comment_id=None,
            session_end_reason="success",
            session_end_summary="",
        )

        await orch._complete_read_only_chat_followup(session)

        orch._registry.update_report.assert_called_once_with(
            "7",
            report_path="/tmp/run-followup-7.json",
            verification_status="passed",
            verification_output="7 passed",
            summary_comment_id=None,
            session_end_reason="success",
            session_end_summary="",
        )
        orch._registry.increment_followup_attempt.assert_called_once_with("7")
        orch._registry.mark_pending_review.assert_called_once_with("7")
        self.assertIn("7", orch._state.pending_review)
        orch._sync_tracker_issue_state.assert_awaited_once_with("7", "pending_review")
        orch.status_dashboard.on_session_complete.assert_called_once_with("7")

    async def test_active_chat_followup_waits_for_current_run_to_finish(self) -> None:
        """A durable follow-up control file must not mutate a live run."""
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            control_dir = workspace / ".orchestrator_control"
            control_dir.mkdir()
            control_file = control_dir / "followup_1.control"
            control_file.write_text(
                "followup\n7\nqueued question\n", encoding="utf-8"
            )
            record = SimpleNamespace(
                status=IssueStatus.RUNNING,
                intent=Intent.NONE,
                intent_source="",
                last_command="",
                touch=MagicMock(),
            )
            orch = Orchestrator.__new__(Orchestrator)
            orch._workspace_root = workspace
            orch._registry = SimpleNamespace(
                _records={"7": record},
                _save=MagicMock(),
            )
            orch._state = SimpleNamespace(
                running={"7": object()},
                completed=set(),
                claimed=set(),
                pending_review=set(),
                failed=set(),
                retry_queue=[],
            )
            orch._sync_tracker_issue_state = AsyncMock()
            # 局部构造绕过 __init__，补绑 C2b 应用侧协作对象。
            from orchestratord.applications.issue_pr.lifecycle import IssueToPrLifecycle

            orch._issue_app = IssueToPrLifecycle(orch)

            await orch._process_control_commands()

            self.assertTrue(control_file.exists())
            self.assertIs(record.status, IssueStatus.RUNNING)
            self.assertIs(record.intent, Intent.NONE)
            orch._sync_tracker_issue_state.assert_not_awaited()

            orch._state.running.clear()
            await orch._process_control_commands()

            self.assertFalse(control_file.exists())
            self.assertIs(record.status, IssueStatus.PENDING)
            self.assertIs(record.intent, Intent.FOLLOWUP)
            orch._sync_tracker_issue_state.assert_awaited_once_with("7", "open")


class TestWorkspaceIgnoreInvariants(unittest.TestCase):
    def test_runtime_artifacts_do_not_make_read_only_followup_dirty(self) -> None:
        """Configured ignore patterns must retain orchestratord internals."""
        with tempfile.TemporaryDirectory() as tmp:
            subprocess.run(["git", "init", "-q", tmp], check=True)
            runtime_dir = Path(tmp) / ".orchestrator_workspace"
            runtime_dir.mkdir()
            (runtime_dir / "README.md").write_text("runtime metadata\n", encoding="utf-8")

            orch = Orchestrator.__new__(Orchestrator)
            orch.git_sync = GitSyncService(
                tracker=MagicMock(),
                branch_prefix="agent/",
                gitignore_patterns=["*.log"],
            )
            orch._sync_gitignore_to_workspace(SimpleNamespace(path=tmp))

            self.assertEqual(get_file_status(tmp), [])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_orchestrator(
    *,
    tracker: Any,
    registry: IssueRegistry,
    workflow: WorkflowConfig | None = None,
) -> Orchestrator:
    orch = Orchestrator.__new__(Orchestrator)
    orch.workflow = workflow or WorkflowConfig()
    orch.tracker = tracker
    orch.workspace = MagicMock()
    orch.agent_runner = MagicMock()
    orch.status_dashboard = MagicMock()
    orch._registry = registry
    orch._state = MagicMock()
    orch._state.running = {}
    orch._state.claimed = set()
    orch._state.max_concurrent_agents = 10
    orch._state.pr_conflict_scan_last_run = 0.0
    orch._log_audit_event = MagicMock()
    return orch


def _make_issue(issue_id: str = "7", branch_name: str | None = "feature/7") -> Issue:
    return Issue(
        id=issue_id,
        identifier=f"ISSUE-{issue_id}",
        title="Test E2E",
        branch_name=branch_name,
    )


_NO_WORKSPACE = object()


def _make_registry_record(
    tmp: Path,
    issue_id: str = "7",
    *,
    pr_number: int | None = 35,
    workspace_path: Any = _NO_WORKSPACE,
    branch_name: str | None = "feature/7",
    base_branch: str = "main",
    has_conflict: bool = False,
    rebase_attempt_count: int = 0,
    conflict_files: tuple[str, ...] = (),
) -> IssueRegistry:
    reg_path = tmp / "r.json"
    reg = IssueRegistry(reg_path)
    # Default to a synthetic temp workspace. Tests that want the
    # "no workspace" precondition pass ``workspace_path=None``
    # explicitly so we record None on the IssueRecord.
    if workspace_path is _NO_WORKSPACE:
        effective_workspace: str | None = str(tmp / "ws")
    else:
        effective_workspace = workspace_path
    reg.register(
        issue_id=issue_id,
        issue_identifier=f"ISSUE-{issue_id}",
        branch_name=branch_name,
        base_branch=base_branch,
        workspace_path=effective_workspace,
    )
    record = reg.get(issue_id)
    assert record is not None
    if effective_workspace is None:
        record.workspace_path = None
    record.pr_number = pr_number
    record.pr_url = f"https://example/pr/{pr_number}" if pr_number else None
    record.has_conflict = has_conflict
    record.conflict_files = conflict_files
    record.rebase_attempt_count = rebase_attempt_count
    reg._save()
    return reg


# ---------------------------------------------------------------------------
# _check_rebase_rate_limit
# ---------------------------------------------------------------------------


class TestCheckRebaseRateLimit(unittest.TestCase):
    def test_under_limit_allows_and_increments(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            reg = _make_registry_record(Path(tmp))
            orch = _make_orchestrator(tracker=MagicMock(), registry=reg)
            issue = _make_issue()
            self.assertTrue(orch._check_rebase_rate_limit(issue))
            reloaded = IssueRegistry(Path(tmp) / "r.json")
            self.assertEqual(reloaded.get("7").rebase_attempt_count, 1)

    def test_at_limit_returns_false(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            reg = _make_registry_record(Path(tmp), rebase_attempt_count=3)
            orch = _make_orchestrator(tracker=MagicMock(), registry=reg)
            issue = _make_issue()
            # Default max_rebase_attempts_per_issue = 3.
            self.assertFalse(orch._check_rebase_rate_limit(issue))
            orch._log_audit_event.assert_called_once()
            call_kwargs = orch._log_audit_event.call_args.kwargs
            self.assertEqual(call_kwargs["event"], "rebase_rejected")

    def test_at_limit_with_force_bypasses(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            reg = _make_registry_record(Path(tmp), rebase_attempt_count=3)
            orch = _make_orchestrator(tracker=MagicMock(), registry=reg)
            issue = _make_issue()
            self.assertTrue(orch._check_rebase_rate_limit(issue, force=True))
            # rebase_rejected NOT called when force bypasses.
            orch._log_audit_event.assert_not_called()


# ---------------------------------------------------------------------------
# _process_rebase_intent
# ---------------------------------------------------------------------------


class TestProcessRebaseIntent(unittest.IsolatedAsyncioTestCase):
    async def test_clean_rebase_clears_conflict(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            reg = _make_registry_record(
                tmp_path,
                has_conflict=True,
                conflict_files=("src/a.py",),
            )
            orch = _make_orchestrator(tracker=MagicMock(), registry=reg)

            clean_result = PRRebaseResult(
                rebased=True,
                has_conflict=False,
                conflict_files=(),
                new_head_sha="abcdef0123456789",
                pushed=True,
                push_method="force_with_lease",
                workspace_clean=True,
            )
            with patch(
                "orchestratord.applications.issue_pr.interpret.rebase_for_pr",
                return_value=clean_result,
            ) as mocked:
                result = await orch._process_rebase_intent(_make_issue())
            self.assertIs(result, clean_result)
            mocked.assert_called_once()
            reloaded = IssueRegistry(tmp_path / "r.json")
            record = reloaded.get("7")
            assert record is not None
            self.assertFalse(record.has_conflict)
            self.assertEqual(list(record.conflict_files), [])
            self.assertEqual(record.commit_sha, "abcdef0123456789")

    async def test_conflict_rebase_marks_conflict(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            reg = _make_registry_record(tmp_path, has_conflict=False)
            orch = _make_orchestrator(tracker=MagicMock(), registry=reg)

            conflict_result = PRRebaseResult(
                rebased=False,
                has_conflict=True,
                conflict_files=("src/plugins/x.py", "src/plugins/y.py"),
                workspace_clean=False,
            )
            with patch(
                "orchestratord.applications.issue_pr.interpret.rebase_for_pr",
                return_value=conflict_result,
            ):
                result = await orch._process_rebase_intent(_make_issue())
            assert result is not None
            self.assertTrue(result.has_conflict)
            reloaded = IssueRegistry(tmp_path / "r.json")
            record = reloaded.get("7")
            assert record is not None
            self.assertTrue(record.has_conflict)
            self.assertEqual(
                tuple(record.conflict_files),
                ("src/plugins/x.py", "src/plugins/y.py"),
            )
            orch._log_audit_event.assert_called()
            # Audit event is "rebase_conflict".
            event_names = [
                call.kwargs.get("event") for call in orch._log_audit_event.call_args_list
            ]
            self.assertIn("rebase_conflict", event_names)

    async def test_no_record_returns_none(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            reg = IssueRegistry(Path(tmp) / "r.json")
            orch = _make_orchestrator(tracker=MagicMock(), registry=reg)
            result = await orch._process_rebase_intent(_make_issue("999"))
            self.assertIsNone(result)

    async def test_missing_workspace_returns_none(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            reg = _make_registry_record(tmp_path, workspace_path=None)
            orch = _make_orchestrator(tracker=MagicMock(), registry=reg)
            result = await orch._process_rebase_intent(_make_issue())
            self.assertIsNone(result)


# ---------------------------------------------------------------------------
# _process_pending_rebase_conflicts
# ---------------------------------------------------------------------------


class TestProcessPendingRebaseConflicts(unittest.IsolatedAsyncioTestCase):
    async def test_no_conflicts_noop(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            reg = _make_registry_record(tmp_path, has_conflict=False)
            tracker = MagicMock()
            orch = _make_orchestrator(tracker=tracker, registry=reg)
            orch.workspace.create_for_issue = AsyncMock()
            orch._launch_rebase_resolution = AsyncMock()
            await orch._process_pending_rebase_conflicts()
            orch._launch_rebase_resolution.assert_not_called()

    async def test_conflict_launches_resolution(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            reg = _make_registry_record(
                tmp_path,
                has_conflict=True,
                conflict_files=("src/x.py",),
            )
            tracker = MagicMock()
            issue = _make_issue()
            tracker.fetch_issue_states_by_ids = AsyncMock(return_value={"7": issue})
            orch = _make_orchestrator(tracker=tracker, registry=reg)
            orch.workspace.create_for_issue = AsyncMock(
                return_value=MagicMock(path=tmp_path / "ws")
            )
            orch.workspace.current_head = AsyncMock(return_value="abc123")
            orch._launch_rebase_resolution = AsyncMock()
            orch._tasks = set()
            orch._state.max_concurrent_agents = 10
            orch._state.running = {}
            await orch._process_pending_rebase_conflicts()
            # The resolution is launched as a background task; yield once
            # so the scheduled task runs before asserting on the mock.
            await asyncio.sleep(0)
            orch._launch_rebase_resolution.assert_awaited_once()
            # First arg is the Issue.
            self.assertEqual(orch._launch_rebase_resolution.await_args.args[0].id, "7")

    async def test_rate_limited_records_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            reg = _make_registry_record(
                tmp_path,
                has_conflict=True,
                rebase_attempt_count=3,  # at limit
            )
            tracker = MagicMock()
            issue = _make_issue()
            tracker.fetch_issue_states_by_ids = AsyncMock(return_value={"7": issue})
            orch = _make_orchestrator(tracker=tracker, registry=reg)
            orch.workspace.create_for_issue = AsyncMock()
            orch._launch_rebase_resolution = AsyncMock()
            orch._state.max_concurrent_agents = 10
            orch._state.running = {}
            await orch._process_pending_rebase_conflicts()
            orch._launch_rebase_resolution.assert_not_called()

    async def test_slot_limiting_prevents_oversubscription(self) -> None:
        """Regression: concurrency slot guard limits rebase launches per poll.

        With ``asyncio.create_task``, ``_state.running`` is only populated
        when each task starts executing — not at task creation.  Without a
        ``launched_this_poll`` counter (mirroring ``_poll_and_dispatch``),
        the loop would launch background tasks for every conflicting record
        in the snapshot, oversubscribing beyond ``max_concurrent_agents``.
        """
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            # Build a registry with 3 conflicting records plus 1 running
            # issue so that only 1 free slot remains.
            reg = _make_registry_record(tmp_path, issue_id="7", has_conflict=True)
            reg.register(
                issue_id="8",
                issue_identifier="ISSUE-8",
                branch_name="feature/8",
                base_branch="main",
            )
            reg.register(
                issue_id="9",
                issue_identifier="ISSUE-9",
                branch_name="feature/9",
                base_branch="main",
            )
            # Mark all 3 as conflicting.
            for rid in ("7", "8", "9"):
                rec = reg.get(rid)
                assert rec is not None
                rec.has_conflict = True
            reg._save()

            # 1 of 10 slots consumed by a running issue → 9 free,
            # but we deliberately set max_concurrent_agents=2 and
            # running={session} so available_slots=1.
            tracker = MagicMock()
            tracker.fetch_issue_states_by_ids = AsyncMock(
                side_effect=lambda ids: {i: _make_issue(issue_id=i) for i in ids}
            )
            orch = _make_orchestrator(tracker=tracker, registry=reg)
            orch.workspace.create_for_issue = AsyncMock(
                return_value=MagicMock(path=tmp_path / "ws")
            )
            orch.workspace.current_head = AsyncMock(return_value="abc123")
            orch._launch_rebase_resolution = AsyncMock()
            orch._tasks = set()
            orch._state.max_concurrent_agents = 2
            # 1 running (non-rebase) issue occupies a slot.
            orch._state.running = {"99": MagicMock()}

            await orch._process_pending_rebase_conflicts()
            await asyncio.sleep(0)

            # With only 1 free slot, at most 1 resolution should launch.
            self.assertEqual(
                orch._launch_rebase_resolution.await_count,
                1,
                f"expected 1 rebase launch (1 free slot, 3 conflicts); "
                f"got {orch._launch_rebase_resolution.await_count} launches — "
                f"slot guard may be missing",
            )

    async def test_hanging_agent_run_does_not_block_poll_loop(self) -> None:
        """Regression (O1 CRITICAL): rebase resolution must not freeze the daemon.

        ``_process_pending_rebase_conflicts`` used to ``await``
        ``_launch_rebase_resolution`` inline, which awaited
        ``agent_runner.run`` with the 30-minute ``run_timeout_ms`` budget —
        freezing the whole poll loop (control commands, retry queue,
        heartbeat, candidate issue fetch) for the duration.

        After the fix the resolution is launched via ``asyncio.create_task``,
        so ``_process_pending_rebase_conflicts`` returns immediately while
        the agent_rebase run hangs in the background: the stop control file
        is consumed and the retry queue advances.
        """
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            reg = _make_registry_record(
                tmp_path,
                has_conflict=True,
                conflict_files=("src/x.py",),
            )
            tracker = MagicMock()
            tracker.fetch_issue_states_by_ids = AsyncMock(
                return_value={"7": _make_issue()}
            )
            orch = _make_orchestrator(tracker=tracker, registry=reg)
            orch.workspace.create_for_issue = AsyncMock(
                return_value=MagicMock(path=tmp_path / "ws")
            )
            orch.workspace.current_head = AsyncMock(return_value="abc123")
            orch._tasks = set()
            orch._issue_tasks = {}
            orch._workspace_root = tmp_path
            orch._state.max_concurrent_agents = 10
            orch._state.running = {}
            orch._state.completed = set()
            orch._state.failed = set()
            orch._state.retry_queue = []
            orch._build_session_sink = MagicMock(return_value=MagicMock())
            orch._emit_im_event = MagicMock()
            orch._log_audit_event = MagicMock()
            orch._rebase_conflict_resolved = AsyncMock(
                return_value=(True, "newhead123")
            )
            orch._clarification_resolver = MagicMock()

            # Fake agent_runner.run that hangs until released — simulates a
            # rebase-resolution session lasting longer than a poll interval
            # (real budget: run_timeout_ms, default 30 minutes).
            run_started = asyncio.Event()
            hang = asyncio.Event()

            async def hanging_run(*args: Any, **kwargs: Any) -> None:
                run_started.set()
                await hang.wait()  # hangs >= 60s in production; released below

            orch.agent_runner.run = hanging_run

            # Must return promptly instead of blocking on the hanging run.
            try:
                await asyncio.wait_for(
                    orch._process_pending_rebase_conflicts(),
                    timeout=5.0,
                )
            except TimeoutError:
                self.fail(
                    "_process_pending_rebase_conflicts blocked the poll loop: "
                    "rebase resolution was awaited inline, not launched as a "
                    "background task"
                )
            # Yield once so the background task starts running.
            await asyncio.sleep(0)
            self.assertTrue(run_started.is_set(), "agent_runner.run was not called")
            self.assertIn(
                "7", orch._state.running, "issue must be in running state"
            )

            # Daemon responsibilities keep advancing while the run hangs.

            # 1) Control command consumption: write a stop control file.
            control_dir = tmp_path / ".orchestrator_control"
            control_dir.mkdir()
            stop_file = control_dir / "stop.control"
            stop_file.write_text("stop\n7\n", encoding="utf-8")
            await orch._process_control_commands()
            self.assertFalse(
                stop_file.exists(), "stop control file was not consumed"
            )

            # 2) Retry queue processing still works.
            retry_issue = _make_issue(issue_id="9", branch_name="feature/9")
            retry_record = SimpleNamespace(
                issue_id="9",
                scheduled_at=time.time() - 10,
                delay_seconds=0,
                attempt=1,
                requeue_count=0,
            )
            orch._state.retry_queue = [retry_record]
            tracker.fetch_issue_states_by_ids = AsyncMock(
                return_value={"9": retry_issue}
            )
            orch._launch_issue = AsyncMock()
            await orch._process_retry_queue()
            orch._launch_issue.assert_awaited_once()

            # 3) Release the run; the done-callback must migrate state via
            #    _finalize_rebase_resolution (conflict cleared).
            hang.set()
            for _ in range(200):
                rec = reg.get("7")
                if rec is not None and not rec.has_conflict:
                    break
                await asyncio.sleep(0.01)
            rec = reg.get("7")
            assert rec is not None
            self.assertFalse(
                rec.has_conflict,
                "conflict not cleared after rebase run completed",
            )
            orch._rebase_conflict_resolved.assert_awaited_once()


# ---------------------------------------------------------------------------
# _process_pr_conflict_scan
# ---------------------------------------------------------------------------


class TestProcessPrConflictScan(unittest.IsolatedAsyncioTestCase):
    async def test_disabled_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            reg = _make_registry_record(tmp_path)
            tracker = MagicMock()
            tracker.fetch_pull_request_mergeable = AsyncMock()
            orch = _make_orchestrator(tracker=tracker, registry=reg)
            await orch._process_pr_conflict_scan()
            tracker.fetch_pull_request_mergeable.assert_not_called()

    async def test_enabled_no_prs_noop(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            reg = _make_registry_record(tmp_path, pr_number=None)
            workflow = WorkflowConfig()
            workflow.pr_conflict_scan = PrConflictScanConfig(enabled=True)
            tracker = MagicMock()
            tracker.fetch_pull_request_mergeable = AsyncMock()
            orch = _make_orchestrator(tracker=tracker, registry=reg, workflow=workflow)
            await orch._process_pr_conflict_scan()
            tracker.fetch_pull_request_mergeable.assert_not_called()

    async def test_enabled_with_conflict_dispatches_rebase(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            reg = _make_registry_record(tmp_path, has_conflict=False)
            # Pin pr_state so the scan does not skip the record on
            # the state filter.
            reg.get("7").pr_state = "open"
            reg._save()
            workflow = WorkflowConfig()
            workflow.pr_conflict_scan = PrConflictScanConfig(enabled=True)
            tracker = MagicMock()
            conflict_status = MergeableStatus(
                mergeable=False,
                mergeable_state="dirty",
                has_conflicts=True,
                behind_by=2,
            )
            tracker.fetch_pull_request_mergeable = AsyncMock(return_value=conflict_status)
            issue = _make_issue()
            tracker.fetch_issue_states_by_ids = AsyncMock(return_value={"7": issue})
            orch = _make_orchestrator(tracker=tracker, registry=reg, workflow=workflow)
            orch._process_rebase_intent = AsyncMock()
            await orch._process_pr_conflict_scan()
            tracker.fetch_pull_request_mergeable.assert_awaited_once()
            orch._process_rebase_intent.assert_awaited_once()

    async def test_gitcode_fallback_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            reg = _make_registry_record(tmp_path, has_conflict=False)
            workflow = WorkflowConfig()
            workflow.pr_conflict_scan = PrConflictScanConfig(enabled=True)
            tracker = MagicMock()
            tracker.fetch_pull_request_mergeable = AsyncMock(return_value=None)
            orch = _make_orchestrator(tracker=tracker, registry=reg, workflow=workflow)
            orch._process_rebase_intent = AsyncMock()
            await orch._process_pr_conflict_scan()
            # None from tracker → daemon scan is a no-op on GitCode.
            orch._process_rebase_intent.assert_not_called()

    async def test_merged_pr_cached_state_skips_without_fetch(self) -> None:
        """Cached ``pr_state=\"merged\"`` → zero-cost skip (no fetch, no rebase)."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            reg = _make_registry_record(tmp_path, has_conflict=False)
            reg.get("7").pr_state = "merged"
            reg._save()
            workflow = WorkflowConfig()
            workflow.pr_conflict_scan = PrConflictScanConfig(enabled=True)
            tracker = MagicMock()
            tracker.fetch_pull_request_mergeable = AsyncMock()
            orch = _make_orchestrator(tracker=tracker, registry=reg, workflow=workflow)
            orch._process_rebase_intent = AsyncMock()
            await orch._process_pr_conflict_scan()
            # The cached state filter must kick in before the remote fetch:
            # a merged PR is never probed again, so no rebase intent and no
            # rate-limit quota burned.
            tracker.fetch_pull_request_mergeable.assert_not_called()
            orch._process_rebase_intent.assert_not_called()

    async def test_merged_pr_detected_from_payload_skips_rebase(self) -> None:
        """Fetched payload with ``state=\"closed\"`` + ``merged=True`` skips rebase
        and writes ``pr_state=\"merged\"`` back to the registry record."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            reg = _make_registry_record(tmp_path, has_conflict=False)
            workflow = WorkflowConfig()
            workflow.pr_conflict_scan = PrConflictScanConfig(enabled=True)
            tracker = MagicMock()
            # GitCode returns "not mergeable" for a merged PR → previously
            # misjudged as a conflict → repeated rebase attempts.
            merged_status = MergeableStatus(
                mergeable=False,
                has_conflicts=True,
                raw={
                    "platform": "github",
                    "payload": {"state": "closed", "merged": True, "mergeable": False},
                },
            )
            tracker.fetch_pull_request_mergeable = AsyncMock(return_value=merged_status)
            tracker.fetch_issue_states_by_ids = AsyncMock(return_value={})
            orch = _make_orchestrator(tracker=tracker, registry=reg, workflow=workflow)
            orch._process_rebase_intent = AsyncMock()
            await orch._process_pr_conflict_scan()
            orch._process_rebase_intent.assert_not_called()
            reloaded = IssueRegistry(tmp_path / "r.json")
            self.assertEqual(reloaded.get("7").pr_state, "merged")

    async def test_open_conflicting_pr_still_rebased(self) -> None:
        """Open + genuinely conflicting PR is still scanned and rebased."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            reg = _make_registry_record(tmp_path, has_conflict=False)
            workflow = WorkflowConfig()
            workflow.pr_conflict_scan = PrConflictScanConfig(enabled=True)
            tracker = MagicMock()
            conflict_status = MergeableStatus(
                mergeable=False,
                mergeable_state="dirty",
                has_conflicts=True,
                behind_by=2,
                raw={
                    "platform": "github",
                    "payload": {"state": "open", "mergeable": False, "mergeable_state": "dirty"},
                },
            )
            tracker.fetch_pull_request_mergeable = AsyncMock(return_value=conflict_status)
            issue = _make_issue()
            tracker.fetch_issue_states_by_ids = AsyncMock(return_value={"7": issue})
            orch = _make_orchestrator(tracker=tracker, registry=reg, workflow=workflow)
            orch._process_rebase_intent = AsyncMock()
            await orch._process_pr_conflict_scan()
            orch._process_rebase_intent.assert_awaited_once()
            reloaded = IssueRegistry(tmp_path / "r.json")
            self.assertEqual(reloaded.get("7").pr_state, "open")

    async def test_terminal_issue_state_skips_rebase(self) -> None:
        """Anti-race guard: an issue already in a terminal tracker state is
        not rebased even when its PR reports conflicts."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            reg = _make_registry_record(tmp_path, has_conflict=False)
            workflow = WorkflowConfig()
            workflow.pr_conflict_scan = PrConflictScanConfig(enabled=True)
            tracker = MagicMock()
            conflict_status = MergeableStatus(
                mergeable=False,
                mergeable_state="dirty",
                has_conflicts=True,
                behind_by=2,
                raw={
                    "platform": "github",
                    "payload": {"state": "open", "mergeable": False, "mergeable_state": "dirty"},
                },
            )
            tracker.fetch_pull_request_mergeable = AsyncMock(return_value=conflict_status)
            tracker.terminal_states = ["closed"]
            closed_issue = _make_issue()
            closed_issue.state = "closed"
            tracker.fetch_issue_states_by_ids = AsyncMock(return_value={"7": closed_issue})
            orch = _make_orchestrator(tracker=tracker, registry=reg, workflow=workflow)
            orch._process_rebase_intent = AsyncMock()
            await orch._process_pr_conflict_scan()
            orch._process_rebase_intent.assert_not_called()


# ---------------------------------------------------------------------------
# _launch_rebase_resolution (completion handling)
# ---------------------------------------------------------------------------


class TestLaunchRebaseResolution(unittest.IsolatedAsyncioTestCase):
    """the agent_rebase run's completion handling.

    Regression coverage for the bug where ``_launch_rebase_resolution``
    popped the session out of ``_state.running`` and did nothing else,
    leaving ``has_conflict`` set forever -> an infinite re-launch loop
    (repeated "Run in progress" comments + 任务已启动/任务完成 oscillation)
    and no PR link in IM. These tests do NOT mock
    ``_launch_rebase_resolution``; they exercise the real method end to
    end (only ``agent_runner.run`` and the git probe are stubbed).
    """

    def _make_orch(
        self,
        tmp: Path,
        *,
        conflict_files: tuple[str, ...] = ("src/x.py",),
        pr_url: str = "https://example/pr/35",
    ) -> tuple[Orchestrator, IssueRegistry]:
        reg = _make_registry_record(
            tmp,
            has_conflict=True,
            conflict_files=conflict_files,
        )
        rec = reg.get("7")
        assert rec is not None
        rec.pr_url = pr_url
        reg._save()
        orch = _make_orchestrator(tracker=MagicMock(), registry=reg)
        orch.git_sync = MagicMock()
        orch.agent_runner = MagicMock()
        orch.agent_runner.run = AsyncMock()
        orch._clarification_resolver = MagicMock()
        # Stub the sink builder + IM/audit plumbing so the test focuses on
        # the completion logic, not the IM delivery stack.
        orch._build_session_sink = MagicMock(return_value=MagicMock())
        orch._emit_im_event = MagicMock()
        orch._rebase_conflict_resolved = AsyncMock()
        orch._tasks = set()
        orch._state.completed = set()
        orch._state.failed = set()
        return orch, reg

    async def test_resolved_clears_conflict_and_emits_pr_link(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            orch, reg = self._make_orch(Path(tmp))
            orch._rebase_conflict_resolved = AsyncMock(return_value=(True, "abc123deadbeef"))
            issue = _make_issue()
            # _launch_rebase_resolution runs the agent (stubbed) and returns
            # the session; the state migration is completed by
            # _finalize_rebase_resolution (invoked via the done-callback
            # wired in _process_pending_rebase_conflicts).
            session = await orch._launch_rebase_resolution(issue)
            await orch._finalize_rebase_resolution(issue, session)

            # has_conflict cleared + new HEAD recorded on the registry.
            rec = reg.get("7")
            assert rec is not None
            self.assertFalse(rec.has_conflict)
            self.assertEqual(rec.commit_sha, "abc123deadbeef")
            # Status transition + dashboard.
            orch.status_dashboard.on_session_complete.assert_called_once_with("7")
            self.assertIn("7", orch._state.completed)
            # A PR-link-bearing pr.updated event was emitted.
            pr_calls = [
                c
                for c in orch._emit_im_event.call_args_list
                if c.args and len(c.args) > 1 and c.args[1] == "pr.updated"
            ]
            self.assertTrue(pr_calls, "pr.updated event not emitted")
            payload = pr_calls[0].args[4]
            self.assertEqual(payload.get("pr"), "https://example/pr/35")
            self.assertEqual(payload.get("commit"), "abc123deadbeef")

    async def test_unresolved_keeps_conflict_and_emits_failure_with_pr(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            orch, reg = self._make_orch(Path(tmp))
            orch._rebase_conflict_resolved = AsyncMock(return_value=(False, None))
            issue = _make_issue()
            session = await orch._launch_rebase_resolution(issue)
            await orch._finalize_rebase_resolution(issue, session)

            rec = reg.get("7")
            assert rec is not None
            self.assertTrue(rec.has_conflict)  # stays set for bounded retry
            self.assertNotIn("7", orch._state.completed)
            orch.status_dashboard.on_session_failed.assert_called_once_with(
                "7", "rebase_unresolved"
            )
            fail_calls = [
                c
                for c in orch._emit_im_event.call_args_list
                if c.args and len(c.args) > 1 and c.args[1] == "issue.failed"
            ]
            self.assertTrue(fail_calls, "issue.failed event not emitted")
            # The failure event still carries the PR link so the operator
            # can jump to the PR and intervene manually.
            self.assertEqual(fail_calls[0].args[4].get("pr"), "https://example/pr/35")

    async def test_prompt_override_uses_rebase_template_with_conflict_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            orch, _reg = self._make_orch(
                Path(tmp), conflict_files=("src/widgets/a.py", "src/widgets/b.py")
            )
            await orch._launch_rebase_resolution(_make_issue())

            # The session handed to agent_runner.run carries the purpose-
            # built rebase prompt (Bug B: previously render_rebase was
            # dead code and the agent ran the generic issue prompt).
            session = orch.agent_runner.run.call_args.args[0]
            prompt = getattr(session, "prompt_override", None)
            self.assertIsNotNone(prompt, "rebase session must set prompt_override")
            self.assertIn("force-with-lease", prompt)
            self.assertIn("src/widgets/a.py", prompt)
            self.assertIn("src/widgets/b.py", prompt)
            self.assertEqual(getattr(session, "run_kind", None), "agent_rebase")


# ---------------------------------------------------------------------------
# _rebase_conflict_resolved (real-git detection)
# ---------------------------------------------------------------------------


class TestRebaseConflictResolved(unittest.IsolatedAsyncioTestCase):
    """git ground-truth detection for ``_rebase_conflict_resolved``.

    The behavioral tests above stub this method; these exercise the real
    git probe so "resolved" actually means "no unmerged files and no
    active rebase state directory" (not just "session.status == completed").
    """

    _ENV = {
        **os.environ,
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_SYSTEM": "/dev/null",
        "GIT_AUTHOR_NAME": "test",
        "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "test",
        "GIT_COMMITTER_EMAIL": "t@example.com",
    }

    @classmethod
    def _init_repo(cls, path: Path) -> None:
        subprocess.check_call(["git", "init", "-q", "-b", "main", str(path)], env=cls._ENV)
        subprocess.check_call(
            ["git", "config", "user.email", "t@example.com"], cwd=path, env=cls._ENV
        )
        subprocess.check_call(["git", "config", "user.name", "test"], cwd=path, env=cls._ENV)
        subprocess.check_call(["git", "config", "commit.gpgsign", "false"], cwd=path, env=cls._ENV)
        (path / "README.md").write_text("init\n", encoding="utf-8")
        subprocess.check_call(["git", "add", "README.md"], cwd=path, env=cls._ENV)
        subprocess.check_call(["git", "commit", "-q", "-m", "init"], cwd=path, env=cls._ENV)

    @classmethod
    def _prepare_conflicted_repo(cls, path: Path, remote: Path) -> str:
        cls._init_repo(path)
        subprocess.check_call(["git", "init", "--bare", "-q", str(remote)], env=cls._ENV)
        subprocess.check_call(
            ["git", "remote", "add", "origin", str(remote)],
            cwd=path,
            env=cls._ENV,
        )
        subprocess.check_call(
            ["git", "push", "-q", "-u", "origin", "main"],
            cwd=path,
            env=cls._ENV,
        )
        subprocess.check_call(
            ["git", "checkout", "-q", "-b", "base2"],
            cwd=path,
            env=cls._ENV,
        )
        (path / "README.md").write_text("base-side\n", encoding="utf-8")
        subprocess.check_call(
            ["git", "commit", "-q", "-am", "base2"],
            cwd=path,
            env=cls._ENV,
        )
        subprocess.check_call(
            ["git", "push", "-q", "-u", "origin", "base2"],
            cwd=path,
            env=cls._ENV,
        )
        subprocess.check_call(
            ["git", "checkout", "-q", "main"],
            cwd=path,
            env=cls._ENV,
        )
        (path / "README.md").write_text("feature-side\n", encoding="utf-8")
        subprocess.check_call(
            ["git", "commit", "-q", "-am", "feature"],
            cwd=path,
            env=cls._ENV,
        )
        subprocess.check_call(
            ["git", "push", "-q", "origin", "main"],
            cwd=path,
            env=cls._ENV,
        )
        previous_head = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=path, env=cls._ENV, text=True
        ).strip()
        rebase = subprocess.run(
            ["git", "rebase", "base2"],
            cwd=path,
            env=cls._ENV,
            capture_output=True,
            text=True,
        )
        if rebase.returncode == 0:
            raise AssertionError("expected a rebase conflict")
        return previous_head

    async def test_clean_repo_without_rebase_evidence_reports_unresolved(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "repo"
            path.mkdir()
            self._init_repo(path)
            orch = Orchestrator.__new__(Orchestrator)
            resolved, new_head = await orch._rebase_conflict_resolved(str(path))
            self.assertFalse(resolved)
            self.assertIsNone(new_head)

    async def test_conflicted_rebase_reports_unresolved(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "repo"
            path.mkdir()
            self._init_repo(path)
            # Diverge on the same line so `git rebase` conflicts and leaves
            # REBASE_HEAD + an unmerged file.
            subprocess.check_call(["git", "checkout", "-q", "-b", "base2"], cwd=path, env=self._ENV)
            (path / "README.md").write_text("base-side\n", encoding="utf-8")
            subprocess.check_call(["git", "commit", "-q", "-am", "base2"], cwd=path, env=self._ENV)
            subprocess.check_call(["git", "checkout", "-q", "main"], cwd=path, env=self._ENV)
            (path / "README.md").write_text("feature-side\n", encoding="utf-8")
            subprocess.check_call(
                ["git", "commit", "-q", "-am", "feature"], cwd=path, env=self._ENV
            )
            r = subprocess.run(
                ["git", "rebase", "base2"],
                cwd=path,
                env=self._ENV,
                capture_output=True,
                text=True,
            )
            assert r.returncode != 0, "expected a rebase conflict"
            orch = Orchestrator.__new__(Orchestrator)
            resolved, new_head = await orch._rebase_conflict_resolved(
                str(path),
                base_branch="base2",
                branch_name="main",
            )
            self.assertFalse(resolved)
            self.assertIsNone(new_head)

    async def test_completed_conflicted_rebase_reports_resolved(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "repo"
            path.mkdir()
            previous_head = self._prepare_conflicted_repo(path, Path(tmp) / "remote.git")
            (path / "README.md").write_text("resolved\n", encoding="utf-8")
            subprocess.check_call(["git", "add", "README.md"], cwd=path, env=self._ENV)
            subprocess.check_call(
                ["git", "-c", "core.editor=true", "rebase", "--continue"],
                cwd=path,
                env=self._ENV,
            )
            subprocess.check_call(
                ["git", "push", "-q", "--force", "origin", "main"],
                cwd=path,
                env=self._ENV,
            )

            # Git retains REBASE_HEAD after a successful rebase. The old
            # implementation treated this pseudo-ref as active rebase state
            # and emitted the false "rebase conflict unresolved" warning.
            stale_rebase_head = subprocess.run(
                ["git", "rev-parse", "--verify", "-q", "REBASE_HEAD"],
                cwd=path,
                env=self._ENV,
                capture_output=True,
                text=True,
            )
            self.assertEqual(stale_rebase_head.returncode, 0)

            orch = Orchestrator.__new__(Orchestrator)
            resolved, new_head = await orch._rebase_conflict_resolved(
                str(path),
                previous_head=previous_head,
                base_branch="base2",
                branch_name="main",
            )
            self.assertTrue(resolved)
            head = subprocess.check_output(
                ["git", "rev-parse", "HEAD"],
                cwd=path,
                env=self._ENV,
                text=True,
            ).strip()
            self.assertEqual(new_head, head)

    async def test_aborted_conflicted_rebase_reports_unresolved(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "repo"
            path.mkdir()
            previous_head = self._prepare_conflicted_repo(path, Path(tmp) / "remote.git")
            subprocess.check_call(["git", "rebase", "--abort"], cwd=path, env=self._ENV)

            orch = Orchestrator.__new__(Orchestrator)
            resolved, new_head = await orch._rebase_conflict_resolved(
                str(path),
                previous_head=previous_head,
                base_branch="base2",
                branch_name="main",
            )

            self.assertFalse(resolved)
            self.assertIsNone(new_head)

    async def test_local_only_rebase_without_push_reports_unresolved(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "repo"
            path.mkdir()
            previous_head = self._prepare_conflicted_repo(path, Path(tmp) / "remote.git")
            (path / "README.md").write_text("resolved\n", encoding="utf-8")
            subprocess.check_call(["git", "add", "README.md"], cwd=path, env=self._ENV)
            subprocess.check_call(
                ["git", "-c", "core.editor=true", "rebase", "--continue"],
                cwd=path,
                env=self._ENV,
            )

            orch = Orchestrator.__new__(Orchestrator)
            resolved, new_head = await orch._rebase_conflict_resolved(
                str(path),
                previous_head=previous_head,
                base_branch="base2",
                branch_name="main",
            )

            self.assertFalse(resolved)
            self.assertIsNone(new_head)

    async def test_remote_base_advance_after_push_reports_unresolved(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "repo"
            path.mkdir()
            previous_head = self._prepare_conflicted_repo(path, Path(tmp) / "remote.git")
            (path / "README.md").write_text("resolved\n", encoding="utf-8")
            subprocess.check_call(["git", "add", "README.md"], cwd=path, env=self._ENV)
            subprocess.check_call(
                ["git", "-c", "core.editor=true", "rebase", "--continue"],
                cwd=path,
                env=self._ENV,
            )
            subprocess.check_call(
                ["git", "push", "-q", "--force", "origin", "main"],
                cwd=path,
                env=self._ENV,
            )
            subprocess.check_call(
                ["git", "checkout", "-q", "base2"],
                cwd=path,
                env=self._ENV,
            )
            (path / "BASE.md").write_text("new target commit\n", encoding="utf-8")
            subprocess.check_call(["git", "add", "BASE.md"], cwd=path, env=self._ENV)
            subprocess.check_call(
                ["git", "commit", "-q", "-m", "advance base"],
                cwd=path,
                env=self._ENV,
            )
            subprocess.check_call(
                ["git", "push", "-q", "origin", "base2"],
                cwd=path,
                env=self._ENV,
            )
            subprocess.check_call(
                ["git", "checkout", "-q", "main"],
                cwd=path,
                env=self._ENV,
            )

            orch = Orchestrator.__new__(Orchestrator)
            resolved, new_head = await orch._rebase_conflict_resolved(
                str(path),
                previous_head=previous_head,
                base_branch="base2",
                branch_name="main",
            )

            self.assertFalse(resolved)
            self.assertIsNone(new_head)

    async def test_missing_workspace_reports_unresolved(self) -> None:
        orch = Orchestrator.__new__(Orchestrator)
        resolved, new_head = await orch._rebase_conflict_resolved(None)
        self.assertFalse(resolved)
        self.assertIsNone(new_head)


# ---------------------------------------------------------------------------
# PENDING_REVIEW / COMPLETED startup recovery (daemon restart)
# ---------------------------------------------------------------------------


class TestRecoverPersistentStates(unittest.IsolatedAsyncioTestCase):
    """Regression for issue #8: PENDING_REVIEW records are lost on restart.

    The candidate-issue poll loop only consults the in-memory ``_state``
    sets (``completed`` / ``pending_review``), which are empty after a
    daemon restart.  Records persisted with ``PENDING_REVIEW`` status must
    be re-hydrated into ``_state.pending_review`` at startup so the issue
    is NOT re-launched on the existing PR branch.
    """

    def _make_state(self) -> SimpleNamespace:
        return SimpleNamespace(
            running={},
            completed=set(),
            claimed=set(),
            pending_review=set(),
            failed=set(),
            retry_queue=[],
            max_concurrent_agents=10,
            poll_interval_ms=30_000,
            poll_check_in_progress=False,
        )

    async def test_recover_restores_pending_review_and_completed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            reg_path = tmp_path / "r.json"

            # Pre-populate a registry that survived a daemon restart.
            seed = IssueRegistry(reg_path)
            seed.register(issue_id="9", issue_identifier="ISSUE-9")
            seed.mark_pending_review("9")
            seed.register(issue_id="10", issue_identifier="ISSUE-10")
            seed.mark_completed("10")
            # A plain PENDING record should NOT be recovered into either set.
            seed.register(issue_id="11", issue_identifier="ISSUE-11")

            orch = _make_orchestrator(
                tracker=MagicMock(), registry=IssueRegistry(reg_path)
            )
            orch._state = self._make_state()

            await orch._recover_persistent_states()

            self.assertIn("9", orch._state.pending_review)
            self.assertIn("10", orch._state.completed)
            self.assertNotIn("11", orch._state.pending_review)
            self.assertNotIn("11", orch._state.completed)

    async def test_pending_review_issue_not_relaunched_on_first_poll(self) -> None:
        """Registry has PENDING_REVIEW record; tracker still lists it open.

        After startup recovery the first poll must NOT re-launch the issue
        (regression: it used to be re-run on the existing PR branch).
        """
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            reg_path = tmp_path / "r.json"
            seed = IssueRegistry(reg_path)
            seed.register(issue_id="7", issue_identifier="ISSUE-7")
            seed.mark_pending_review("7")

            issue = _make_issue()
            tracker = MagicMock()
            tracker.fetch_candidate_issues = AsyncMock(return_value=[issue])

            orch = _make_orchestrator(tracker=tracker, registry=IssueRegistry(reg_path))
            orch._state = self._make_state()
            orch._clarification_gate = None
            orch._launch_issue = AsyncMock()
            orch._resolve_intent = AsyncMock(return_value=(Intent.NONE, None, None))
            orch._emit_im_event = MagicMock()
            orch._broadcast_clarification_status = MagicMock()
            orch._workflow_mtime_ns = MagicMock(return_value=None)
            orch._dynamic_tracker_config_mtime_ns = None
            orch._refresh_dynamic_title_prefix_filter = MagicMock()
            orch._process_control_commands = AsyncMock()
            orch._clarification_resolver = MagicMock()
            orch._clarification_resolver.poll_clarification_answers = AsyncMock()
            orch._process_retry_queue = AsyncMock()
            orch._process_escalated_issues = AsyncMock()
            orch._process_review_feedback = AsyncMock()
            orch._process_pending_rebase_conflicts = AsyncMock()
            orch._process_pr_conflict_scan = AsyncMock()
            from orchestratord.applications.issue_pr.provider import IssuePrWorkProvider

            orch._work_provider = IssuePrWorkProvider(orch)

            # Simulate daemon startup recovery.
            await orch._recover_persistent_states()
            self.assertIn("7", orch._state.pending_review)

            await orch._poll_and_dispatch()

            orch._launch_issue.assert_not_awaited()
            self.assertIn("7", orch._state.pending_review)


class TestIssuePrInterpretationReviewFollowup(unittest.TestCase):
    """Regression: _uses_review_feedback_followup must be a @staticmethod.

    Issue #42: after the method moved from Orchestrator into
    IssuePrInterpretation, the @staticmethod decorator was dropped.  Instance-
    style calls like lifecycle.post_viz_gate / provider._provide both invoke
    ``self._interpretation._uses_review_feedback_followup(record)``, which
    bound the instance as the first argument and raised TypeError.
    """

    def _make_interpretation(self) -> IssuePrInterpretation:
        return IssuePrInterpretation(host=SimpleNamespace())

    def test_instance_call_chat_followup_returns_false(self) -> None:
        """Instance-style call records that do NOT need a review round."""
        record = SimpleNamespace(intent=Intent.FOLLOWUP, intent_source="chat")
        interpretation = self._make_interpretation()
        # Before the @staticmethod fix this raises TypeError.
        self.assertFalse(interpretation._uses_review_feedback_followup(record))

    def test_instance_call_command_followup_returns_true(self) -> None:
        """Instance-style call records that DO need a review round."""
        record = SimpleNamespace(intent=Intent.FOLLOWUP, intent_source="cli")
        interpretation = self._make_interpretation()
        self.assertTrue(interpretation._uses_review_feedback_followup(record))

    def test_instance_call_none_record_returns_false(self) -> None:
        """None record (no registry entry) never triggers review feedback."""
        interpretation = self._make_interpretation()
        self.assertFalse(interpretation._uses_review_feedback_followup(None))

    def test_class_call_matches_orchestrator_shim(self) -> None:
        """Class-level calls must stay consistent with Orchestrator shim."""
        chat = SimpleNamespace(intent=Intent.FOLLOWUP, intent_source="chat")
        cli = SimpleNamespace(intent=Intent.FOLLOWUP, intent_source="cli")
        self.assertFalse(IssuePrInterpretation._uses_review_feedback_followup(chat))
        self.assertTrue(IssuePrInterpretation._uses_review_feedback_followup(cli))
        self.assertFalse(Orchestrator._uses_review_feedback_followup(chat))
        self.assertTrue(Orchestrator._uses_review_feedback_followup(cli))


class TestPostVizGateReviewFollowupCrash(unittest.IsolatedAsyncioTestCase):
    """Regression: post_viz_gate must not crash on followup records.

    Issue #42 traceback hit lifecycle.post_viz_gate:
    ``self._interpretation._uses_review_feedback_followup(followup_record)``
    raised TypeError when the method lost its @staticmethod.  The bare
    instance-call tests above prove the decorator; this test drives the
    REAL call site so the gate intercepts the exact crash path the issue
    described (previously uncovered: "post_viz_gate 的 launch 链路无测试").
    """

    def _make_lifecycle(self, record: Any) -> IssueToPrLifecycle:
        host = SimpleNamespace()
        host._registry = SimpleNamespace(get=lambda issue_id: record)
        lifecycle = IssueToPrLifecycle(host)
        # Do not fetch real PR feedback / launch a real follow-up run.
        lifecycle._interpretation._launch_followup_with_pending_reviews = AsyncMock(
            return_value=True
        )
        # Do not wire a real intent session for the non-followup branch.
        lifecycle._interpretation._prepare_intent_session = MagicMock()
        return lifecycle

    async def test_command_followup_returns_false_without_typeerror(self) -> None:
        """Command followup: post_viz_gate must not raise TypeError."""
        record = SimpleNamespace(intent=Intent.FOLLOWUP, intent_source="cli")
        lifecycle = self._make_lifecycle(record)
        session = SimpleNamespace(issue=Issue(id="42"), run_kind=None)
        # Before the @staticmethod fix this raised
        # TypeError: takes 1 positional argument but 2 were given.
        handled = await lifecycle.post_viz_gate(session)
        self.assertFalse(handled)
        lifecycle._interpretation._launch_followup_with_pending_reviews.assert_awaited_once_with(
            session.issue
        )

    async def test_none_record_returns_true_without_typeerror(self) -> None:
        """None record: gate is skipped, session wiring proceeds, no crash."""
        lifecycle = self._make_lifecycle(None)
        session = SimpleNamespace(issue=Issue(id="42"), run_kind=None)
        handled = await lifecycle.post_viz_gate(session)
        self.assertTrue(handled)
        lifecycle._interpretation._launch_followup_with_pending_reviews.assert_not_awaited()
        lifecycle._interpretation._prepare_intent_session.assert_called_once_with(
            session
        )


if __name__ == "__main__":
    unittest.main()
