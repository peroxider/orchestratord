"""Convert between Issue (tracker domain) and AgentTask (orchestration domain).

This is the **only** place where Issue fields are mapped to AgentTask
fields.  If a new task kind needs Issue data, it goes through this
function — not by accessing Issue directly.

See ``DESIGN_agent_task_abstraction.md`` for the full architecture.
"""

from __future__ import annotations

from ..agent.task import AgentTask
from .issue import Issue


def issue_to_agent_task(
    issue: Issue,
    *,
    attempt: int = 1,
    previous_run_ids: list[str] | None = None,
    workspace_path: str = "",
    max_turns: int | None = None,
    timeout_seconds: float | None = None,
    clarification_question: str | None = None,
    clarification_answer: str | None = None,
    clarification_source: str | None = None,
    conflict_files: tuple[str, ...] | None = None,
    prompt_override: str | None = None,
) -> AgentTask:
    """Convert an Issue to a generic AgentTask.

    This is the **only** place where Issue fields are mapped to
    AgentTask fields.  If a new task kind needs Issue data, it goes
    through this function — not by accessing Issue directly.
    """
    context: dict[str, object] = {
        "issue_id": issue.id,
        "issue_identifier": issue.identifier,
        "issue_url": issue.url,
        "issue_state": issue.state,
        "issue_author_login": issue.author_login,
        "issue_branch_name": issue.branch_name,
        "issue_python_executable": issue.python_executable,
        # Business-owned stage policy.  The workflow engine treats these as
        # opaque instructions; Git/PR semantics do not live in StageRunner.
        "stage_instructions": [
            "You may use git add and git commit on the current branch.",
            "Do not run git push; the issue-to-PR application handles synchronization.",
            "Do not switch branches or create pull requests from an agent stage.",
        ],
    }

    if clarification_question:
        context["clarification_question"] = clarification_question
    if clarification_answer:
        context["clarification_answer"] = clarification_answer
    if clarification_source:
        context["clarification_source"] = clarification_source
    if conflict_files:
        context["conflict_files"] = list(conflict_files)

    return AgentTask(
        id=issue.id or "",
        kind="issue",
        title=issue.title or "",
        description=issue.description or "",
        context=context,
        workspace_path=workspace_path,
        labels=list(issue.labels) if issue.labels else [],
        priority=issue.priority,
        attempt=attempt,
        previous_run_ids=list(previous_run_ids) if previous_run_ids else [],
        max_turns=max_turns,
        timeout_seconds=timeout_seconds,
        prompt_override=prompt_override,
    )
