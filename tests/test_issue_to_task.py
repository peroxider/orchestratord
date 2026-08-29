from __future__ import annotations

from orchestratord.issue import Issue
from orchestratord.issue_to_task import issue_to_agent_task


def test_issue_to_agent_task_maps_business_data_to_opaque_context() -> None:
    issue = Issue(
        id="id-1",
        identifier="ENG-1",
        title="Fix parser",
        description="Broken for empty input",
        labels=["bug"],
        priority=1,
        url="https://tracker/ENG-1",
        state="In Progress",
        author_login="ada",
        branch_name="fix/parser",
        python_executable="/venv/bin/python",
    )

    task = issue_to_agent_task(
        issue,
        attempt=2,
        previous_run_ids=["run-1"],
        workspace_path="/work/repo",
        max_turns=7,
        timeout_seconds=30,
        clarification_question="Which format?",
        clarification_answer="JSON",
        clarification_source="comment",
        conflict_files=("parser.py",),
    )

    assert task.id == "id-1"
    assert task.kind == "issue"
    assert task.title == "Fix parser"
    assert task.workspace_path == "/work/repo"
    assert task.previous_run_ids == ["run-1"]
    assert task.context == {
        "issue_id": "id-1",
        "issue_identifier": "ENG-1",
        "issue_url": "https://tracker/ENG-1",
        "issue_state": "In Progress",
        "issue_author_login": "ada",
        "issue_branch_name": "fix/parser",
        "issue_python_executable": "/venv/bin/python",
        "clarification_question": "Which format?",
        "clarification_answer": "JSON",
        "clarification_source": "comment",
        "conflict_files": ["parser.py"],
    }
