"""Issue→PR 声明式工作流执行 — issue→PR 应用的业务策略（DESIGN §4.1/§3.2）。

B3 自宿主 ``_run_issue_with_workflow`` 机械迁入：``self.``→``host.`` 改写后
逐行等值。通过 WorkflowOrchestrator 按 workflow.yaml 定义的 DAG 阶段执行
issue，每个阶段由 AgentRunner 驱动的合成 Issue 执行。模块顶层只 import
共享基础设施与低层业务模块，不 import orchestrator / orchestration_subsystem
/ app（import 时序契约）。
"""

from __future__ import annotations

import logging
from typing import Any

from orchestratord.git.utils import get_default_branch
from orchestratord.session_state import AgentSession

logger = logging.getLogger(__name__)


async def run_issue_with_workflow(
    host: Any,
    session: AgentSession,
    progress_sink: Any,
) -> None:
    """使用声明式工作流引擎处理 issue。

    通过 WorkflowOrchestrator 按 workflow.yaml 定义的 DAG 阶段
    执行 issue，每个阶段由 AgentRunner 驱动的合成 Issue 执行。
    """
    workflow_orch = host._workflow_orchestrator
    if workflow_orch is None:
        logger.error("_run_issue_with_workflow called but no workflow orchestrator")
        session.status = "failed"
        return

    logger.info(
        "Running workflow for issue %s: %s",
        session.issue.identifier,
        session.issue.title,
    )

    # 确保 workspace 在 issue 分支上（非主分支）。
    # 保留的工作区可能还在 main 或上一次运行的分支上，
    # 必须在 workflow 执行前切换到正确的 issue 分支。
    try:
        work_branch = host.git_sync._ensure_work_branch(
            str(session.workspace.path),
            session.issue,
            session.base_branch or get_default_branch(str(session.workspace.path)),
        )
        logger.info(
            "Workflow workspace on branch: %s (issue=%s)",
            work_branch,
            session.issue.identifier,
        )
    except Exception as exc:
        logger.warning(
            "Failed to ensure work branch for workflow issue %s: %s",
            session.issue.id,
            exc,
        )

    # 将编排器的 ProgressSink 注入工作流引擎，
    # 使阶段进度实时反映到 StatusDashboard
    workflow_orch.set_progress_sink(progress_sink)
    workflow_orch._stage_runner._progress_reporter = progress_sink

    try:
        # run_for_issue 已删除（机制域不得内嵌业务转换，DESIGN §3.2）：
        # Issue→AgentTask 的业务映射留在业务侧完成后走通用入口。
        from orchestratord.issue_registry.task_mapping import issue_to_agent_task

        result = await workflow_orch.run_for_task(
            issue_to_agent_task(
                session.issue,
                workspace_path=str(session.workspace.path),
            )
        )
    except Exception as exc:
        logger.exception("Workflow execution failed for issue %s", session.issue.id)
        session.status = "failed"
        session.output_text = str(exc)
        return

    # 将阶段输出存储到 session，供 git_sync 写入 PR body
    session.workflow_stage_outputs = {}
    for stage_id, stage_result in result.stage_results.items():
        if stage_result.outputs:
            session.workflow_stage_outputs[stage_id] = {
                "phase": getattr(workflow_orch.schema.get_stage(stage_id), "phase", ""),
                "name": getattr(
                    workflow_orch.schema.get_stage(stage_id), "name", f"Stage {stage_id}"
                ),
                "output": stage_result.outputs[0] if stage_result.outputs else "",
            }

    if result.success:
        session.status = "completed"
        session.output_text = (
            f"Workflow '{result.workflow_name}' completed: "
            f"{result.completed_stages}/{result.total_stages} stages, "
            f"cost=${result.total_cost_usd:.4f}, "
            f"duration={result.total_duration_seconds:.1f}s"
        )
    else:
        session.status = "failed"
        session.output_text = (
            f"Workflow '{result.workflow_name}' failed at stage "
            f"{result.completed_stages}/{result.total_stages}: {result.error}"
        )

    # 工作流引擎在 per-stage session 上设置 _snapshot_backend，
    # 外层 session 不会被设置，run report 的 Backend 字段会显示 n/a。
    # 从 agent_runner 回填，确保 report 能正确展示后端名称。
    _backend = getattr(host.agent_runner, "backend", None)
    if _backend is not None:
        session._snapshot_backend = getattr(_backend, "name", None) or ""
        from orchestratord.cost.estimator import resolve_model_alias

        _agent_config = getattr(host.agent_runner, "agent_config", None)
        session._snapshot_model = resolve_model_alias(
            getattr(_agent_config, "model", None) or "",
            getattr(_agent_config, "model_aliases", None) or {},
        )
        session._snapshot_provider = (
            getattr(host.agent_runner.agent_config, "provider", None)
            or getattr(_backend, "name", "")
        )

    host._update_run_diagnostics(session)


__all__ = ["run_issue_with_workflow"]
