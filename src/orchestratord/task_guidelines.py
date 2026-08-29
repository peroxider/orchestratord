"""Task V2 / Logical Kanban guidance for multi-step agents.

This guidance is surfaced so agents see the same TaskCreate/TaskUpdate
semantics whether they are running in an interactive session or were
launched by the orchestrator. The text is internalized here rather than
defined by the orchestrator, since it is fundamentally a prompt instruction
that the task assigner has the right to define.
"""

from __future__ import annotations


def get_task_guidelines() -> str:
    return (
        "\n\nTask tracking guidelines:\n"
        "- For multi-step work, create structured tasks with TaskCreate and track them "
        "with TaskList.\n"
        "- Declare dependencies with TaskUpdate `addBlockedBy` before starting work.\n"
        "- Only mark a task `in_progress` after TaskGet confirms its `blockedBy` list is empty.\n"
        "- Only mark a task `completed` when the work is fully done and tests pass.\n"
        "- If a task becomes blocked, use TaskList/TaskGet to read the blocked reason and "
        "repair suggestions, then act on them."
    )
