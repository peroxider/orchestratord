"""GATE 回滚处理器。

处理 GATE 拒绝后的回滚逻辑：
- 确定回滚目标阶段
- 恢复工作区快照
- 更新工作流状态
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import RollbackError
from .rollback import RollbackManager, RollbackTarget
from .workflow_state import StageNode, StageStatus, WorkflowState

logger = logging.getLogger(__name__)


# ── GATE 回滚结果 ────────────────────────────────────────────────────


@dataclass
class GateRollbackResult:
    """GATE 回滚结果。"""

    success: bool
    target_stage_id: int
    target_stage_name: str
    reason: str
    snapshot_restored: bool = False


# ── GATE 回滚处理器 ──────────────────────────────────────────────────


class GateRollbackHandler:
    """GATE 回滚处理器。

    当 GATE 阶段被拒绝时，执行回滚操作。
    复用 Human Review Gate 的 workspace 保留策略。
    """

    def __init__(
        self,
        rollback_manager: RollbackManager,
        workspace_dir: str | Path = "",
    ) -> None:
        self._rollback = rollback_manager
        self._workspace_dir = Path(workspace_dir) if workspace_dir else Path(".")

    def resolve_gate_rollback(
        self,
        stage: StageNode,
        state: WorkflowState,
        rejection_reason: str = "",
    ) -> RollbackTarget:
        """解析 GATE 拒绝后的回滚目标。

        优先级:
        1. stage.gate_rollback_to 显式指定
        2. stage.depends_on 第一个依赖阶段
        3. 工作流起始阶段

        Args:
            stage: GATE 阶段节点
            state: 工作流状态
            rejection_reason: 拒绝原因

        Returns:
            RollbackTarget: 回滚目标
        """
        # 显式指定
        if stage.gate_rollback_to is not None:
            target_id = int(stage.gate_rollback_to)
            target_snapshot = self._rollback.get_snapshot(target_id)
            return RollbackTarget(
                stage_id=target_id,
                stage_name=f"stage-{target_id}",
                reason=f"GATE rejected: {rejection_reason} (explicit rollback_to={target_id})",
                snapshot=target_snapshot,
            )

        # 依赖阶段
        if stage.depends_on:
            target_id = stage.depends_on[0]
            target_snapshot = self._rollback.get_snapshot(target_id)
            return RollbackTarget(
                stage_id=target_id,
                stage_name=f"stage-{target_id}",
                reason=f"GATE rejected: {rejection_reason} (rollback to dependency)",
                snapshot=target_snapshot,
            )

        raise RollbackError(
            f"No rollback target for GATE stage {stage.id}",
            stage_id=stage.id,
        )

    def execute_rollback(
        self,
        target: RollbackTarget,
        state: WorkflowState,
        failed_stage_id: int,
    ) -> GateRollbackResult:
        """执行 GATE 回滚。

        1. 恢复目标阶段快照
        2. 更新 WorkflowState
        3. 返回回滚结果

        Args:
            target: 回滚目标
            state: 工作流状态
            failed_stage_id: 失败的 GATE 阶段 ID

        Returns:
            GateRollbackResult: 回滚结果
        """
        snapshot_restored = False

        try:
            if target.snapshot is not None:
                snapshot_restored = self._rollback.restore_snapshot(target.stage_id)
        except Exception as exc:
            logger.warning("Snapshot restore failed, continuing with state rollback only: %s", exc)

        # 更新状态
        self._rollback.update_state_on_rollback(state, target.stage_id, failed_stage_id)

        # 标记 GATE 阶段为已拒绝
        if failed_stage_id in state.stage_statuses:
            state.stage_statuses[failed_stage_id] = StageStatus.GATE_REJECTED

        return GateRollbackResult(
            success=True,
            target_stage_id=target.stage_id,
            target_stage_name=target.stage_name,
            reason=target.reason,
            snapshot_restored=snapshot_restored,
        )

    def determine_dag_index(
        self,
        target: RollbackTarget,
        dag_order: list[int],
    ) -> int:
        """确定回滚目标在 DAG 中的索引。

        Args:
            target: 回滚目标
            dag_order: DAG 顺序列表

        Returns:
            目标阶段在 DAG 中的索引
        """
        try:
            return dag_order.index(target.stage_id)
        except ValueError:
            # 如果目标不在 DAG 中，从第一个阶段开始
            logger.warning(
                "Rollback target %s not in DAG order, starting from beginning",
                target.stage_id,
            )
            return 0

    async def handle_gate_rejection(
        self,
        stage: StageNode,
        state: WorkflowState,
        dag_order: list[int],
        rejection_reason: str = "",
    ) -> tuple[int, GateRollbackResult]:
        """处理 GATE 拒绝的完整流程。

        解析目标 -> 执行回滚 -> 返回新的 DAG 索引。

        Args:
            stage: GATE 阶段节点
            state: 工作流状态
            dag_order: DAG 顺序列表
            rejection_reason: 拒绝原因

        Returns:
            (新的 DAG 索引, 回滚结果)
        """
        target = self.resolve_gate_rollback(stage, state, rejection_reason)
        result = self.execute_rollback(target, state, stage.id)
        new_index = self.determine_dag_index(target, dag_order)

        logger.info(
            "GATE rollback: stage %s rejected, rolling back to stage %s (index %s)",
            stage.id,
            target.stage_id,
            new_index,
        )

        return new_index, result
