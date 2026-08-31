"""Backend-neutral one-shot workflow runtime."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from .agent.runner import AgentTaskRunner
from .agent.task import AgentTask
from .workflow_engine.cost import CostBudget
from .workflow_engine.engine import (
    DeclarativeWorkflowEngine,
    EngineConfig,
    WorkflowResult,
    WorkflowSchema,
)
from .workflow_engine.stage_runner import StageRunner
from .workflow_engine.workflow_state import WorkflowState


class WorkflowRunner:
    """Execute a workflow definition for a generic task.

    This composition root has no tracker, Issue, Git-sync, or PR dependency.
    Business applications may wrap it with their own acquisition and
    publication lifecycle.
    """

    def __init__(
        self,
        workflow_file: str | Path,
        *,
        task_runner: AgentTaskRunner | None,
        workspace_dir: str | Path,
        max_cost_usd: float = 50.0,
        max_concurrent_stages: int = 1,
        default_timeout_seconds: int = 1800,
        llm_client: Any = None,
        control_state: Callable[[], str | None] | None = None,
    ) -> None:
        self.schema = WorkflowSchema.from_yaml(workflow_file)
        self.workspace_dir = str(Path(workspace_dir).expanduser().resolve())
        self.engine = DeclarativeWorkflowEngine(
            self.schema,
            EngineConfig(
                cost_budget=CostBudget(max_total_usd=max_cost_usd),
                max_concurrent_stages=max(1, max_concurrent_stages),
                default_timeout_seconds=default_timeout_seconds,
                workspace_dir=self.workspace_dir,
                llm_client=llm_client,
                control_state=control_state,
            ),
        )
        self.engine.set_stage_runner(
            StageRunner(task_runner=task_runner, workspace_dir=self.workspace_dir)
        )

    async def run(self, task: AgentTask, *, from_stage: int | None = None) -> WorkflowResult:
        self.engine.state = WorkflowState(
            workflow_name=self.schema.name,
            workflow_version=self.schema.version,
        )
        self.engine.state.run_context = {
            "id": task.id,
            "kind": task.kind,
            "title": task.title,
            "description": task.description,
            "labels": list(task.labels),
            "task": task,
        }
        return await self.engine.execute(from_stage=from_stage)
