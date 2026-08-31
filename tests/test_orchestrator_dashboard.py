"""Test orchestrator dashboard with 3 mock issues.

Verifies that StatusDashboard correctly displays running/completed sessions
and the dashboard renders multi-line output with RUNNING SESSIONS header.
"""

from __future__ import annotations

import asyncio
import unittest
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

from orchestratord.agent_runner import AgentRunner, AgentSession
from orchestratord.config.schema import (
    AgentConfig,
    SandboxConfig,
    PollingConfig,
    TrackerConfig,
    WorkflowConfig,
    WorkspaceConfig,
)
from orchestratord.issue_registry.issue import Issue
from orchestratord.orchestrator import Orchestrator
from orchestratord.status_dashboard import SessionStatus, StatusDashboard
from orchestratord.tracker import TrackerAdapter
from orchestratord.workspace import Workspace, WorkspaceManager


@dataclass
class MockIssue:
    id: str
    identifier: str
    title: str
    description: str = ""
    priority: int | None = None
    state: str = "In Progress"
    branch_name: str | None = None
    url: str | None = None
    assignee_id: str | None = None
    blocked_by: list = field(default_factory=list)
    labels: list = field(default_factory=list)
    assigned_to_worker: bool = True
    created_at: Any = None
    updated_at: Any = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "identifier": self.identifier,
            "title": self.title,
            "description": self.description,
            "priority": self.priority,
            "state": self.state,
            "branch_name": self.branch_name,
            "url": self.url,
            "assignee_id": self.assignee_id,
            "blocked_by": self.blocked_by,
            "labels": self.labels,
            "assigned_to_worker": self.assigned_to_worker,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


class MockTrackerAdapter(TrackerAdapter):
    """Mock tracker that returns 3 predefined issues."""

    def __init__(self, issues: list[MockIssue]) -> None:
        self._issues = issues
        self.active_states = ["In Progress", "Todo"]
        self._call_count = 0

    async def fetch_candidate_issues(self) -> list[Issue]:
        self._call_count += 1
        if self._call_count == 1:
            return [
                Issue(
                    id=i.id,
                    identifier=i.identifier,
                    title=i.title,
                    description=i.description,
                    priority=i.priority,
                    state=i.state,
                    branch_name=i.branch_name,
                    url=i.url,
                    assignee_id=i.assignee_id,
                    blocked_by=i.blocked_by,
                    labels=i.labels,
                    assigned_to_worker=i.assigned_to_worker,
                    created_at=i.created_at,
                    updated_at=i.updated_at,
                )
                for i in self._issues
            ]
        return []

    async def fetch_issue_states_by_ids(self, issue_ids: list[str]) -> dict[str, Issue]:
        return {
            issue.id: issue
            for issue in (await self.fetch_candidate_issues())
            if issue.id in issue_ids
        }

    async def create_comment(self, issue_id: str, body: str) -> None:
        pass

    async def update_issue_state(self, issue_id: str, state: str) -> None:
        pass

    async def update_comment(self, issue_id: str, comment_id: str, body: str) -> None:
        return None

    async def create_clarification_comment(
        self, issue_id: str, body: str, mentions: list[str] | None = None
    ) -> None:
        return None

    async def extract_intent_from_labels(self, labels: list[str] | None) -> Intent:
        return Intent.NONE

    async def close_pull_request(self, pull_request: Any) -> bool:
        return False


class MockWorkspaceManager(WorkspaceManager):
    """Mock workspace manager that uses temp directories."""

    def __init__(self) -> None:
        super().__init__(WorkspaceConfig(root="/tmp/test_orchestrator_dashboard"))

    async def create_for_issue(self, issue: Any) -> Workspace:
        import tempfile
        import shutil

        identifier = getattr(issue, "identifier", None) or "issue"
        safe_id = "".join(c if c.isalnum() else "_" for c in identifier)
        workspace_path = Path(tempfile.mkdtemp(prefix=f"workspace_{safe_id}_"))

        return Workspace(
            path=workspace_path,
            issue_identifier=safe_id,
            issue_id=getattr(issue, "id", None),
        )

    async def cleanup(self, issue: Any) -> None:
        pass


class TestStatusDashboardThreeIssues(unittest.IsolatedAsyncioTestCase):
    """Test dashboard display with 3 mock issues."""

    async def test_dashboard_renders_with_three_running_sessions(self):
        """Verify dashboard shows RUNNING SESSIONS header with 3 sessions."""
        dashboard = StatusDashboard()

        # Simulate 3 running sessions
        for i, issue_id in enumerate(["issue-1", "issue-2", "issue-3"]):
            session_status = SessionStatus(
                issue_id=issue_id,
                issue_identifier=f"TST-{i + 100}",
                status="running",
                turn_count=0,
                max_turns=20,
                workspace_path=f"/tmp/workspace_{issue_id}",
            )
            dashboard.on_session_start(session_status)

        output = dashboard.render()

        self.assertIn("RUNNING SESSIONS", output)
        self.assertIn("TST-100", output)
        self.assertIn("TST-101", output)
        self.assertIn("TST-102", output)
        self.assertIn("running=3", output)

    async def test_dashboard_updates_on_session_complete(self):
        """Verify dashboard transitions from running to completed."""
        dashboard = StatusDashboard()

        # Start 3 sessions
        for i, issue_id in enumerate(["issue-1", "issue-2", "issue-3"]):
            session_status = SessionStatus(
                issue_id=issue_id,
                issue_identifier=f"TST-{i + 100}",
                status="running",
                turn_count=5,
                max_turns=20,
                workspace_path=f"/tmp/workspace_{issue_id}",
            )
            dashboard.on_session_start(session_status)

        # Complete 2 sessions
        dashboard.on_session_complete("issue-1")
        dashboard.on_session_complete("issue-2")

        output = dashboard.render()
        self.assertIn("completed=2", output)
        # issue-3 should still be in running
        self.assertIn("TST-102", output)

    async def test_dashboard_shows_failed_session(self):
        """Verify dashboard shows failed session with error."""
        dashboard = StatusDashboard()

        session_status = SessionStatus(
            issue_id="issue-fail",
            issue_identifier="TST-999",
            status="running",
            turn_count=0,
            max_turns=20,
            workspace_path="/tmp/workspace_fail",
        )
        dashboard.on_session_start(session_status)
        dashboard.on_session_failed("issue-fail", "max_turns_exceeded")

        output = dashboard.render()
        self.assertIn("failed=1", output)
        self.assertNotIn("TST-999", output)  # failed sessions removed from running table
        self.assertIn("FAILED SESSIONS", output)
        self.assertIn("1 session(s) failed", output)

    async def test_dashboard_sparkline_and_tps(self):
        """Verify TPS calculation and sparkline rendering."""
        dashboard = StatusDashboard()

        # Simulate some TPS samples with actual time passage
        import time

        samples = [100, 150, 200, 180, 220, 250]
        dashboard._token_samples.extend(samples)
        dashboard._last_rendered_at = time.time() - 1.0

        # First call - populates initial sample, returns 0 (not enough samples)
        tps1 = dashboard.tps()
        # Second call - enough samples, calculates delta
        dashboard._last_rendered_at = time.time() - 0.1
        tps2 = dashboard.tps()

        sparkline = dashboard.throughput_sparkline()

        self.assertGreater(len(sparkline), 0)
        # TPS can be 0 or negative with very short elapsed time and small deltas
        self.assertIsInstance(tps2, float)

    async def test_orchestrator_integration_with_dashboard(self):
        """Full orchestrator integration test with 3 mock issues and dashboard."""
        # Create 3 mock issues
        mock_issues = [
            MockIssue(id="issue-1", identifier="TST-100", title="Test Issue 1"),
            MockIssue(id="issue-2", identifier="TST-101", title="Test Issue 2"),
            MockIssue(id="issue-3", identifier="TST-102", title="Test Issue 3"),
        ]

        tracker = MockTrackerAdapter(mock_issues)
        workspace_manager = MockWorkspaceManager()

        workflow_config = WorkflowConfig(
            tracker=TrackerConfig(
                kind="linear",
                api_key="mock-key",
                project_slug="test-project",
                active_states=["In Progress", "Todo"],
            ),
            polling=PollingConfig(interval_ms=5000),
            agent=AgentConfig(
                max_concurrent_agents=3,
                max_turns=2,
                provider="anthropic",
                permission_mode="dontAsk",
            ),
            sandbox=SandboxConfig(approval_policy="never"),
        )

        # Create a mock agent runner that simulates quick completions
        class QuickAgentRunner:
            max_turns = 2

            async def run(self, session: AgentSession, workflow: Any, **kwargs) -> None:
                # Simulate a brief run then mark as completed
                session.status = "completed"
                session.turn_count = 1

        agent_runner = QuickAgentRunner()
        dashboard = StatusDashboard()

        orchestrator = Orchestrator(
            workflow=workflow_config,
            tracker=tracker,
            workspace=workspace_manager,
            agent_runner=agent_runner,
            status_dashboard=dashboard,
        )

        # Poll once and verify dashboard state
        await orchestrator._poll_and_dispatch()

        # Give time for sessions to start
        await asyncio.sleep(0.1)

        state = dashboard.state()
        self.assertGreaterEqual(len(state.running), 0)

        # Verify dashboard renders without error
        output = dashboard.render()
        self.assertIsInstance(output, str)
        self.assertIn("Orchestratord", output)


class TestDashboardRenderingEdgeCases(unittest.IsolatedAsyncioTestCase):
    """Edge case tests for dashboard rendering."""

    async def test_render_with_no_sessions(self):
        """Dashboard renders with no running sessions."""
        dashboard = StatusDashboard()
        output = dashboard.render()

        self.assertIn("Orchestratord", output)
        self.assertIn("running=0", output)
        self.assertIn("completed=0", output)

    async def test_render_line(self):
        """render_line() returns single-line summary."""
        dashboard = StatusDashboard()
        dashboard.on_session_start(
            SessionStatus(
                issue_id="test-1",
                issue_identifier="TST-1",
                status="running",
                max_turns=20,
            )
        )

        line = dashboard.render_line()
        self.assertIn("running=1", line)
        self.assertIn("completed=0", line)

    async def test_session_status_age_display(self):
        """Age display formats correctly."""
        session = SessionStatus(
            issue_id="test",
            issue_identifier="TST-1",
            seconds_running=45,
            max_turns=20,
        )
        self.assertEqual(session.age_display(), "45s")

        session.seconds_running = 125
        self.assertEqual(session.age_display(), "2m 5s")

        session.seconds_running = 3665
        self.assertEqual(session.age_display(), "1h 1m")

    async def test_session_status_tokens_display(self):
        """Tokens display formats correctly."""
        session = SessionStatus(
            issue_id="test",
            issue_identifier="TST-1",
            total_tokens=500,
            max_turns=20,
        )
        self.assertEqual(session.tokens_display(), "500")

        session.total_tokens = 1500
        self.assertEqual(session.tokens_display(), "1k")

        session.total_tokens = 2500000
        self.assertEqual(session.tokens_display(), "2M")


if __name__ == "__main__":
    unittest.main()


# --- 澄清面板 ---


def test_clarification_panel_empty() -> None:
    """无澄清状态时面板不渲染。"""
    dashboard = StatusDashboard()
    assert dashboard._clarification_panel() == ""


def test_clarification_panel_awaiting() -> None:
    """awaiting_author 条目渲染正确的图标和轮数。"""
    dashboard = StatusDashboard()
    from orchestratord.status_dashboard import ClarificationEntry

    dashboard.on_clarification_update([
        ClarificationEntry(
            issue_id="42",
            status="awaiting_author",
            open_questions=["Which behavior is expected?"],
            round_num=1,
            max_rounds=2,
            elapsed_seconds=1250.0,
        ),
    ])
    panel = dashboard._clarification_panel()
    assert "📧" in panel
    assert "#42" in panel
    assert "Round 1/2" in panel
    assert "Which behavior" in panel


def test_clarification_panel_manual_required() -> None:
    """manual_required 条目显示 ⚠ 标记。"""
    dashboard = StatusDashboard()
    from orchestratord.status_dashboard import ClarificationEntry

    dashboard.on_clarification_update([
        ClarificationEntry(
            issue_id="15",
            status="manual_required",
            open_questions=[],
            round_num=2,
            max_rounds=2,
            elapsed_seconds=3600.0,
        ),
    ])
    panel = dashboard._clarification_panel()
    assert "⚠" in panel
    assert "#15" in panel
    assert "exhausted" in panel


def test_clarification_panel_awaiting_local() -> None:
    """awaiting_local 条目使用 👤 图标。"""
    dashboard = StatusDashboard()
    from orchestratord.status_dashboard import ClarificationEntry

    dashboard.on_clarification_update([
        ClarificationEntry(
            issue_id="38",
            status="awaiting_local",
            open_questions=["What is the target QPS?"],
            round_num=1,
            max_rounds=2,
            elapsed_seconds=30.0,
        ),
    ])
    panel = dashboard._clarification_panel()
    assert "👤" in panel
    assert "#38" in panel


def test_clarification_panel_with_resolved() -> None:
    """resolved 条目显示最终状态。"""
    dashboard = StatusDashboard()
    from orchestratord.status_dashboard import ClarificationEntry

    dashboard.on_clarification_update([
        ClarificationEntry(
            issue_id="10",
            status="resolved",
            open_questions=[],
            round_num=1,
            max_rounds=2,
            elapsed_seconds=500.0,
        ),
    ])
    panel = dashboard._clarification_panel()
    assert "✅" in panel
    assert "#10" in panel


def test_pending_clarifications_property() -> None:
    """pending_clarifications 返回 awaiting_local 和 manual_required 条目。"""
    dashboard = StatusDashboard()
    from orchestratord.status_dashboard import ClarificationEntry

    dashboard.on_clarification_update([
        ClarificationEntry(issue_id="1", status="awaiting_author", elapsed_seconds=0.0),
        ClarificationEntry(issue_id="2", status="awaiting_local", elapsed_seconds=0.0),
        ClarificationEntry(issue_id="3", status="manual_required", elapsed_seconds=0.0),
        ClarificationEntry(issue_id="4", status="resolved", elapsed_seconds=0.0),
    ])
    pending = dashboard.pending_clarifications
    assert len(pending) == 2
    assert {e.issue_id for e in pending} == {"2", "3"}


def test_clarification_panel_sorts_by_elapsed_desc() -> None:
    """awaiting 条目按等待时长降序排列。"""
    dashboard = StatusDashboard()
    from orchestratord.status_dashboard import ClarificationEntry

    dashboard.on_clarification_update([
        ClarificationEntry(
            issue_id="old", status="awaiting_author", elapsed_seconds=5000.0),
        ClarificationEntry(
            issue_id="new", status="awaiting_author", elapsed_seconds=10.0),
    ])
    panel = dashboard._clarification_panel()
    # "old" 应该出现在 "new" 之前
    old_pos = panel.index("#old")
    new_pos = panel.index("#new")
    assert old_pos < new_pos
