"""A replacement follow-up run must answer the current operator request."""

from types import SimpleNamespace

import pytest

from orchestratord.agent.task import AgentTask
from orchestratord.prompt_builder import PromptBuilder


@pytest.mark.parametrize("split", [False, True])
def test_followup_keeps_old_task_as_context_and_current_request_last(monkeypatch, tmp_path, split):
    constraints = "Do not modify files."
    template = constraints + "\n" + (PromptBuilder.USER_MESSAGE_MARKER if split else "")
    template += "\n{{ task.description }}"
    monkeypatch.setattr(
        "orchestratord.prompt_builder.get_workflow_store",
        lambda: SimpleNamespace(current=lambda: (None, template)),
    )
    monkeypatch.setattr("orchestratord.prompt_builder._get_workspace_diff", lambda _: None)
    request = "Now explain the result; do not run the original command again."
    hints = tmp_path / ".operator_hints.md"
    hints.write_text(request, encoding="utf-8")
    task = AgentTask(id="one", kind="issue", title="Original task", description="Run original command.")
    session = SimpleNamespace(run_kind="agent_followup", workspace=SimpleNamespace(path=tmp_path))

    system, user = PromptBuilder.render_parts(task, session=session)

    assert user.count(request) == 1
    assert user.endswith(request)
    assert "Previous task context" in user
    assert user.index("Run original command.") < user.index("Current operator request")
    assert request not in system
    assert constraints in (system if split else user)
    assert hints.read_text(encoding="utf-8") == ""


def test_initial_run_keeps_operator_guidance_without_reclassifying_task(monkeypatch, tmp_path):
    monkeypatch.setattr("orchestratord.prompt_builder._get_workspace_diff", lambda _: None)
    (tmp_path / ".operator_hints.md").write_text("Use the existing fixture.", encoding="utf-8")
    task = AgentTask(id="one", kind="issue", title="Original task", description="Run original command.")
    session = SimpleNamespace(run_kind="agent", workspace=SimpleNamespace(path=tmp_path))
    prompt = PromptBuilder.render(task, session=session)
    assert "Operator Hints" in prompt
    assert "Previous task context" not in prompt
