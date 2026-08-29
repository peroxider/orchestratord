from __future__ import annotations

from orchestratord.agent_task import AgentTask, AgentTaskResult


def test_agent_task_exposes_only_render_safe_template_data() -> None:
    context = {"issue_identifier": "ENG-42", "nested": {"value": "x"}}
    task = AgentTask(
        id="42",
        kind="issue",
        title="Ship it",
        description="Make the thing",
        labels=["bug"],
        priority=2,
        attempt=3,
        context=context,
    )

    rendered = task.to_template_dict()

    assert rendered == {
        "id": "42",
        "kind": "issue",
        "title": "Ship it",
        "description": "Make the thing",
        "labels": ["bug"],
        "priority": 2,
        "attempt": 3,
        "context": context,
    }
    assert rendered["context"] is not context


def test_agent_task_result_status_helpers() -> None:
    assert AgentTaskResult(task_id="a").is_success
    assert AgentTaskResult(task_id="a", status="failed").is_terminal_failure
    assert AgentTaskResult(task_id="a", status="premise_not_met").is_terminal_failure
    assert not AgentTaskResult(task_id="a", status="stagnation").is_terminal_failure
