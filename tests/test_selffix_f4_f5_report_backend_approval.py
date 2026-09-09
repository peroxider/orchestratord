"""Regression tests for F4/F5 selffix: report Backend field + approval_policy default.

F4: run report Backend field shows n/a when it should show the backend name.
F5: default approval_policy (dict) causes AskApprovalPolicy noise; change to "never".
"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from orchestratord.kernel.approval import (
    AskApprovalPolicy,
    NeverApprovalPolicy,
    ToolCallEvent,
    resolve_approval_policy,
)
from orchestratord.config.schema import SandboxConfig
from orchestratord.orchestrator import Orchestrator
from orchestratord.report_writer import RunReport, _render_markdown, write
from orchestratord.session_state import RunSession, RunSubject

# ── F4: report Backend field ──────────────────────────────────────────


class TestReportBackendField(unittest.TestCase):
    """F4: run report Backend field must show the backend name, not n/a."""

    def test_report_writer_backend_in_markdown(self) -> None:
        """report_writer.write() with backend= produces markdown containing the name."""
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp)
            issue = SimpleNamespace(id="42", identifier="ISSUE-42", title="Test")
            result = write(
                run_id="run-001",
                workspace_path=ws,
                tracker="test",
                owner="owner",
                repo="repo",
                issue=issue,
                status="completed",
                backend="opencode",
                model="gpt-4",
                output_text="done",
            )
            md = Path(result.workspace_markdown_path).read_text(encoding="utf-8")
            self.assertIn("- Backend: `opencode`", md)
            self.assertNotIn("n/a", md.split("Backend")[1].split("\n")[0] if "Backend" in md else "")

    def test_report_writer_backend_none_falls_back_to_n_a(self) -> None:
        """report_writer.write() without backend still shows n/a (no crash)."""
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp)
            issue = SimpleNamespace(id="42", identifier="ISSUE-42", title="Test")
            result = write(
                run_id="run-002",
                workspace_path=ws,
                tracker="test",
                owner="owner",
                repo="repo",
                issue=issue,
                status="completed",
                backend=None,
                output_text="done",
            )
            md = Path(result.workspace_markdown_path).read_text(encoding="utf-8")
            self.assertIn("- Backend: `n/a`", md)

    def test_render_markdown_with_backend(self) -> None:
        """_render_markdown includes backend name when set."""
        report = RunReport(
            run_id="r1",
            tracker="test",
            owner="o",
            repo="r",
            issue_id="1",
            issue_identifier="I-1",
            issue_title="t",
            status="ok",
            branch_name=None,
            base_branch=None,
            commit_sha=None,
            pr_number=None,
            pr_url=None,
            turn_count=0,
            tool_count=0,
            verification_status=None,
            verification_output=None,
            output_excerpt="",
            backend="opencode",
        )
        md = _render_markdown(report)
        self.assertIn("- Backend: `opencode`", md)

    def test_render_markdown_without_backend(self) -> None:
        """_render_markdown falls back to n/a when backend is None."""
        report = RunReport(
            run_id="r2",
            tracker="test",
            owner="o",
            repo="r",
            issue_id="1",
            issue_identifier="I-1",
            issue_title="t",
            status="ok",
            branch_name=None,
            base_branch=None,
            commit_sha=None,
            pr_number=None,
            pr_url=None,
            turn_count=0,
            tool_count=0,
            verification_status=None,
            verification_output=None,
            output_excerpt="",
            backend=None,
        )
        md = _render_markdown(report)
        self.assertIn("- Backend: `n/a`", md)


class TestWorkflowPathBackfillsSnapshotBackend(unittest.TestCase):
    """F4: _run_issue_with_workflow must backfill session._snapshot_backend.

    The workflow engine runs stages through per-stage sessions; the outer
    session's _snapshot_backend would otherwise be empty, causing the
    run report to show n/a.
    """

    def test_workflow_path_sets_snapshot_backend(self) -> None:
        """_run_issue_with_workflow sets _snapshot_backend from agent_runner."""
        # Build a mock agent_runner with a backend that has .name.
        mock_backend = MagicMock()
        mock_backend.name = "opencode"
        mock_agent_runner = MagicMock()
        mock_agent_runner.backend = mock_backend
        mock_agent_runner.agent_config = MagicMock()
        mock_agent_runner.agent_config.model = "gpt-4"
        mock_agent_runner.agent_config.provider = ""

        # Build a mock WorkflowOrchestrator whose run_for_task() succeeds.
        mock_workflow_orch = MagicMock()
        mock_workflow_result = MagicMock()
        mock_workflow_result.success = True
        mock_workflow_result.workflow_name = "test"
        mock_workflow_result.completed_stages = 1
        mock_workflow_result.total_stages = 1
        mock_workflow_result.total_cost_usd = 0.0
        mock_workflow_result.total_duration_seconds = 1.0
        mock_workflow_result.error = None
        mock_workflow_result.stage_results = {}
        mock_workflow_orch.set_progress_sink = MagicMock()
        mock_workflow_orch._stage_runner = MagicMock()

        # Create a session with empty _snapshot_backend.
        session = RunSession(
            subject=RunSubject(id="1", identifier="I-1", title="t"),
            workspace=SimpleNamespace(path=Path("/tmp")),
        )

        # Create the orchestrator with the mock agent_runner, then
        # inject the mock workflow orchestrator.
        mock_self = MagicMock(spec=Orchestrator)
        mock_self.agent_runner = mock_agent_runner
        mock_self._workflow_orchestrator = mock_workflow_orch
        mock_self.git_sync = MagicMock()

        # Call the method directly. P6 removed run_for_issue: the
        # business side maps Issue → AgentTask and awaits the generic
        # run_for_task entry, so mock that.
        async def _mock_run_for_task(*args, **kwargs):
            return mock_workflow_result

        mock_workflow_orch.run_for_task = _mock_run_for_task

        async def _run():
            await Orchestrator._run_issue_with_workflow(
                mock_self, session, MagicMock()
            )

        asyncio.run(_run())

        # Verify the snapshot fields were backfilled.
        self.assertEqual(session._snapshot_backend, "opencode")
        self.assertEqual(session._snapshot_model, "gpt-4")


# ── F5: default approval_policy ───────────────────────────────────────


class TestDefaultApprovalPolicy(unittest.TestCase):
    """F5: default sandbox.approval_policy must be 'never' (autonomous daemon)."""

    def test_default_is_never(self) -> None:
        """SandboxConfig().approval_policy == 'never'."""
        self.assertEqual(SandboxConfig().approval_policy, "never")

    def test_default_resolves_to_never_policy(self) -> None:
        """resolve_approval_policy(default config, no agent) → NeverApprovalPolicy."""
        policy = resolve_approval_policy(SandboxConfig(), None)
        self.assertIsInstance(policy, NeverApprovalPolicy)

    def test_default_resolves_to_never_with_agent(self) -> None:
        """resolve_approval_policy(default config, with agent) → NeverApprovalPolicy."""
        agent = SimpleNamespace(permission_mode="bypassPermissions")
        policy = resolve_approval_policy(SandboxConfig(), agent)
        self.assertIsInstance(policy, NeverApprovalPolicy)

    def test_explicit_ask_still_works(self) -> None:
        """Explicit approval_policy='ask' still returns AskApprovalPolicy."""
        sandbox = SimpleNamespace(approval_policy="ask")
        agent = SimpleNamespace(permission_mode="bypassPermissions")
        policy = resolve_approval_policy(sandbox, agent)
        self.assertIsInstance(policy, AskApprovalPolicy)

    def test_explicit_never_still_works(self) -> None:
        """Explicit approval_policy='never' still returns NeverApprovalPolicy."""
        sandbox = SimpleNamespace(approval_policy="never")
        agent = SimpleNamespace(permission_mode="dontAsk")
        policy = resolve_approval_policy(sandbox, agent)
        self.assertIsInstance(policy, NeverApprovalPolicy)

    def test_explicit_dict_still_fails_closed(self) -> None:
        """Explicit structured dict approval_policy still returns AskApprovalPolicy."""
        sandbox = SimpleNamespace(
            approval_policy={"reject": {"sandbox_approval": False, "rules": True}}
        )
        agent = SimpleNamespace(permission_mode="bypassPermissions")
        policy = resolve_approval_policy(sandbox, agent)
        self.assertIsInstance(policy, AskApprovalPolicy)


class TestAskApprovalPolicyWording(unittest.TestCase):
    """F5: AskApprovalPolicy deny reason must not be misleading."""

    def test_ask_policy_wording_updated(self) -> None:
        """AskApprovalPolicy.evaluate() uses non-misleading deny reason."""
        policy = AskApprovalPolicy()
        event = ToolCallEvent(tool_name="write")
        ctx = {"permission_mode": "dontAsk"}
        policy.evaluate(event, ctx)
        self.assertFalse(event.is_approved)
        self.assertIn("post-hoc audit", event._deny_reason or "")
        self.assertNotIn("not supported", event._deny_reason or "")

    def test_never_policy_auto_approves(self) -> None:
        """NeverApprovalPolicy always approves."""
        policy = NeverApprovalPolicy()
        event = ToolCallEvent(tool_name="write")
        ctx = {}
        approved = policy.evaluate(event, ctx)
        self.assertTrue(approved)
        self.assertTrue(event.is_approved)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()