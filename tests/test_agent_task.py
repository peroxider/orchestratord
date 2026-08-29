"""Tests for the AgentTask / AgentTaskResult / ProgressEvent data model.

Per ``DESIGN_agent_task_abstraction.md`` §16: ``AgentTask.to_template_dict()``
and ``AgentTaskResult`` property semantics.
"""

from __future__ import annotations

import pytest

from orchestratord.agent_task import AgentTask, AgentTaskResult, ProgressEvent, ProgressEventKind


class TestAgentTask:
    def test_defaults(self):
        task = AgentTask(id="t1")
        assert task.kind == "generic"
        assert task.title == ""
        assert task.description == ""
        assert task.context == {}
        assert task.workspace_path == ""
        assert task.labels == []
        assert task.priority is None
        assert task.attempt == 1
        assert task.previous_run_ids == []
        assert task.max_turns is None
        assert task.timeout_seconds is None
        assert task.prompt_override is None

    def test_to_template_dict(self):
        task = AgentTask(
            id="ISSUE-42",
            kind="issue",
            title="Fix login bug",
            description="Session cookie expires too early.",
            labels=["bug", "auth"],
            priority=2,
            attempt=3,
            context={"issue_id": "42", "issue_identifier": "ISSUE-42"},
        )
        d = task.to_template_dict()
        assert d["id"] == "ISSUE-42"
        assert d["kind"] == "issue"
        assert d["title"] == "Fix login bug"
        assert d["description"] == "Session cookie expires too early."
        assert d["labels"] == ["bug", "auth"]
        assert d["priority"] == 2
        assert d["attempt"] == 3
        assert d["context"] == {"issue_id": "42", "issue_identifier": "ISSUE-42"}
        # Template renderers read through ``task.context.<key>``.
        assert d["context"]["issue_identifier"] == "ISSUE-42"

    def test_to_template_dict_context_is_a_copy(self):
        task = AgentTask(id="t1", context={"a": 1})
        d = task.to_template_dict()
        d["context"]["a"] = 999
        assert task.context["a"] == 1

    def test_kinds_carried_verbatim(self):
        for kind in ("issue", "workflow_stage", "review_followup", "agent_rebase", "ci_fix"):
            assert AgentTask(id="t", kind=kind).kind == kind


class TestAgentTaskResult:
    def test_is_success_only_for_completed(self):
        assert AgentTaskResult(task_id="t", status="completed").is_success
        for status in ("failed", "stagnation", "interrupted", "premise_not_met"):
            assert not AgentTaskResult(task_id="t", status=status).is_success

    def test_is_terminal_failure_membership(self):
        terminal = {
            "failed",
            "premise_not_met",
            "no_changes_produced",
            "empty_branch_no_commits",
        }
        for status in terminal:
            assert AgentTaskResult(task_id="t", status=status).is_terminal_failure
        for status in ("completed", "interrupted", "rate_limited", "stagnation"):
            assert not AgentTaskResult(task_id="t", status=status).is_terminal_failure

    def test_defaults(self):
        r = AgentTaskResult(task_id="t1")
        assert r.kind == "generic"
        assert r.status == "completed"
        assert r.output_text == ""
        assert r.turn_count == 0
        assert r.tool_count == 0
        assert r.cost_usd == 0.0
        assert r.error is None
        assert r.report_path is None
        assert r.run_id is None


class TestProgressEvent:
    def test_kinds_enum_values(self):
        assert ProgressEventKind.TEXT.value == "text"
        assert ProgressEventKind.TEXT_DELTA.value == "text_delta"
        assert ProgressEventKind.TOOL_CALL.value == "tool_call"
        assert ProgressEventKind.TOOL_RESULT.value == "tool_result"
        assert ProgressEventKind.TURN_COMPLETE.value == "turn_complete"
        assert ProgressEventKind.SESSION_COMPLETE.value == "session_complete"
        assert ProgressEventKind.ERROR.value == "error"

    def test_event_defaults(self):
        e = ProgressEvent(kind=ProgressEventKind.TEXT, task_id="t1")
        assert e.turn_number is None
        assert e.tool_count is None
        assert e.text == ""
        assert e.tool_name == ""
        assert e.call_id == ""
        assert e.metadata == {}

    def test_event_carries_payload(self):
        e = ProgressEvent(
            kind=ProgressEventKind.TOOL_CALL,
            task_id="t1",
            turn_number=3,
            tool_name="Read",
            call_id="call-1",
            metadata={"path": "src/main.py"},
        )
        assert e.turn_number == 3
        assert e.tool_name == "Read"
        assert e.call_id == "call-1"
        assert e.metadata["path"] == "src/main.py"


def test_dataclass_round_trip_immutability_surface():
    """Dataclass fields remain simple values — no hidden required args."""
    with pytest.raises(TypeError):
        AgentTask()  # type: ignore[call-arg]  # id is required
