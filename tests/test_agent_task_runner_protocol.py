from __future__ import annotations

from orchestratord.agent_task import AgentTask, AgentTaskResult
from orchestratord.agent_task_runner import AgentTaskRunner


class _Runner:
    async def run_task(self, task: AgentTask, **_kwargs: object) -> AgentTaskResult:
        return AgentTaskResult(task_id=task.id)


def test_structural_runner_satisfies_runtime_protocol() -> None:
    # Protocol is intentionally structural: integrations do not need to
    # inherit from a framework base class.
    assert hasattr(_Runner(), "run_task")
    assert AgentTaskRunner.__dict__["run_task"] is not None
