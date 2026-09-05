"""Terminal diagnostics must survive without the last SSE/transcript event."""

from types import SimpleNamespace

from orchestratord.issue_registry import IssueRecord, IssueRegistry
from orchestratord.orchestrator import Orchestrator
from orchestratord.run_read_model import RunReadModel


def test_stopped_followup_is_truthful_from_registry_alone(tmp_path):
    registry = IssueRegistry(tmp_path / ".orchestratord_issue_registry.json")
    registry._records["task"] = IssueRecord(issue_id="task", issue_identifier="TASK")
    orch = Orchestrator.__new__(Orchestrator)
    orch._registry = registry
    session = SimpleNamespace(
        issue=SimpleNamespace(id="task"), run_id="run", status="failed",
        session_end_reason="operator_stop", session_end_summary="Stopped by operator",
        pause_reason="", completed_at=10, output_text="",
        _snapshot_backend="dsh", _snapshot_provider="openai-codex",
    )
    orch._update_run_diagnostics(session)
    registry.mark_failed("task")
    registry.flush()
    reloaded = IssueRegistry(registry._path)
    assert reloaded.get("task").session_end_reason == "operator_stop"
    issue = RunReadModel(tmp_path).read({})["issues"][0]
    assert issue["status"] == "stopped"
    assert issue["display"]["agent_state"] == "stopped"
    assert issue["execution"]["backend"] == "dsh"
