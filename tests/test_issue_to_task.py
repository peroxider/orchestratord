"""Tests for the Issue → AgentTask adapter.

Per ``DESIGN_agent_task_abstraction.md`` §5 / §16: ``issue_to_agent_task()``
is the only place where Issue fields are mapped to AgentTask fields.
"""

from __future__ import annotations

from orchestratord.agent.task import AgentTask
from orchestratord.config.schema import WorkflowConfig
from orchestratord.issue_registry.issue import Issue
from orchestratord.issue_registry.task_mapping import issue_to_agent_task
from orchestratord.prompt_builder import PromptBuilder
from orchestratord.workflow_store import WorkflowStore, get_workflow_store


def _sample_issue() -> Issue:
    return Issue(
        id="123",
        identifier="ISSUE-42",
        title="Fix auth timeout",
        description="Session expires after 60s.",
        priority=5,
        state="open",
        branch_name="fix/auth-timeout",
        url="https://git.example.com/org/repo/issues/42",
        author_login="alice",
        python_executable="/usr/bin/python3",
        labels=["bug", "auth"],
    )


class TestFieldMapping:
    def test_identity_fields(self):
        task = issue_to_agent_task(_sample_issue())
        assert isinstance(task, AgentTask)
        assert task.id == "123"
        assert task.kind == "issue"
        assert task.title == "Fix auth timeout"
        assert task.description == "Session expires after 60s."
        assert task.priority == 5
        assert task.labels == ["bug", "auth"]

    def test_context_issue_fields(self):
        task = issue_to_agent_task(_sample_issue())
        ctx = task.context
        assert ctx["issue_id"] == "123"
        assert ctx["issue_identifier"] == "ISSUE-42"
        assert ctx["issue_url"] == "https://git.example.com/org/repo/issues/42"
        assert ctx["issue_state"] == "open"
        assert ctx["issue_author_login"] == "alice"
        assert ctx["issue_branch_name"] == "fix/auth-timeout"
        assert ctx["issue_python_executable"] == "/usr/bin/python3"

    def test_defaults_for_empty_issue(self):
        task = issue_to_agent_task(Issue())
        assert task.id == ""
        assert task.title == ""
        assert task.description == ""
        assert task.kind == "issue"
        assert task.labels == []
        assert task.priority is None
        assert task.attempt == 1
        assert task.workspace_path == ""


class TestOptionalParameters:
    def test_attempt_and_previous_run_ids(self):
        task = issue_to_agent_task(
            _sample_issue(),
            attempt=3,
            previous_run_ids=["r1", "r2"],
        )
        assert task.attempt == 3
        assert task.previous_run_ids == ["r1", "r2"]

    def test_workspace_and_lifecycle_hints(self):
        task = issue_to_agent_task(
            _sample_issue(),
            workspace_path="/tmp/ws",
            max_turns=50,
            timeout_seconds=1800.0,
        )
        assert task.workspace_path == "/tmp/ws"
        assert task.max_turns == 50
        assert task.timeout_seconds == 1800.0

    def test_clarification_context(self):
        task = issue_to_agent_task(
            _sample_issue(),
            clarification_question="Which platform?",
            clarification_answer="Linux",
            clarification_source="operator",
        )
        assert task.context["clarification_question"] == "Which platform?"
        assert task.context["clarification_answer"] == "Linux"
        assert task.context["clarification_source"] == "operator"

    def test_conflict_files(self):
        task = issue_to_agent_task(_sample_issue(), conflict_files=("a.py", "b.py"))
        assert task.context["conflict_files"] == ["a.py", "b.py"]

    def test_prompt_override(self):
        task = issue_to_agent_task(_sample_issue(), prompt_override="custom prompt")
        assert task.prompt_override == "custom prompt"

    def test_clarification_fields_absent_when_not_given(self):
        task = issue_to_agent_task(_sample_issue())
        assert "clarification_question" not in task.context
        assert "clarification_answer" not in task.context
        assert "clarification_source" not in task.context
        assert "conflict_files" not in task.context


def test_round_trip_via_to_template_dict():
    """The rendered template dict keeps the issue context reachable."""
    issue = _sample_issue()
    task = issue_to_agent_task(issue)
    d = task.to_template_dict()
    assert d["title"] == issue.title
    assert d["context"]["issue_identifier"] == issue.identifier
    assert d["context"]["issue_id"] == issue.id


def test_agent_task_renders_legacy_issue_template_namespace() -> None:
    WorkflowStore.reset()
    store = get_workflow_store()
    store._config = WorkflowConfig()
    store._prompt_template = (
        "legacy={{ issue.identifier }}|{{ issue.title }}|"
        "{{ issue.description }} task={{ task.title }}"
    )
    try:
        rendered = PromptBuilder.render(issue_to_agent_task(_sample_issue()))
    finally:
        WorkflowStore.reset()

    assert rendered.startswith(
        "legacy=ISSUE-42|Fix auth timeout|Session expires after 60s. "
        "task=Fix auth timeout"
    )
