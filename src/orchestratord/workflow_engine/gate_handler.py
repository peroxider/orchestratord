"""GATE 门禁处理器。

处理工作流中的 GATE 阶段——人类审批、自动阈值、回滚。
三种审批模式: manual, auto, threshold。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from .validators import ContractValidator
from .workflow_state import StageNode, StageResult, StageStatus, WorkflowState

logger = logging.getLogger(__name__)


class GateMode(str, Enum):
    MANUAL = "manual"
    AUTO = "auto"
    THRESHOLD = "threshold"


@dataclass
class GateResult:
    """GATE 处理结果。"""

    approved: bool
    mode: GateMode
    reason: str = ""
    score: float | None = None
    stage_result: StageResult | None = None


class GateHandler:
    """GATE 门禁处理器。

    复用:
    - ClarificationQueue (manual 模式)
    - Human Review Gate (审批流程)
    """

    def __init__(
        self,
        clarification_queue: Any = None,
        journal: Any = None,
        workspace_dir: str = "",
        llm_client: Any = None,
    ) -> None:
        self._clarification_queue = clarification_queue
        self._journal = journal
        self._validator = ContractValidator(
            workspace_dir=workspace_dir,
            llm_client=llm_client,
        )

    async def process(
        self,
        stage_node: StageNode,
        state: WorkflowState,
        stage_result: StageResult,
    ) -> GateResult:
        """处理 GATE 阶段。

        根据 gate_mode 选择审批策略。
        """
        mode = GateMode(stage_node.gate_mode) if stage_node.gate_mode else GateMode.MANUAL

        if mode == GateMode.AUTO:
            return await self._process_auto(stage_node, state, stage_result)
        elif mode == GateMode.THRESHOLD:
            return await self._process_threshold(stage_node, state, stage_result)
        else:
            return await self._process_manual(stage_node, state, stage_result)

    async def _process_manual(
        self,
        stage_node: StageNode,
        state: WorkflowState,
        stage_result: StageResult,
    ) -> GateResult:
        """Manual 审批模式。

        通过 ClarificationQueue 暂停工作流，等待人类审批。
        """
        if self._clarification_queue is not None:
            try:
                self._clarification_queue.enqueue(
                    issue_id=f"workflow-{state.workflow_name}",
                    issue_identifier=f"stage-{stage_node.id}",
                    question=f"Approve stage {stage_node.id}: {stage_node.name}?",
                    options=["approve", "reject"],
                    context_summary=f"Workflow: {state.workflow_name}, Stage: {stage_node.name}",
                )
            except Exception as exc:
                logger.warning("Failed to enqueue clarification: %s", exc)

        return GateResult(
            approved=False,
            mode=GateMode.MANUAL,
            reason=f"GATE stage {stage_node.id} awaiting manual approval",
            stage_result=StageResult(
                stage_id=stage_node.id,
                status=StageStatus.GATE_PENDING,
            ),
        )

    async def _process_auto(
        self,
        stage_node: StageNode,
        state: WorkflowState,
        stage_result: StageResult,
    ) -> GateResult:
        """Auto 审批模式。

        基于 ValidatorSpec 自动判定：所有 validator 通过即 approve。
        """
        if not stage_node.validators:
            logger.info("Auto-gate stage %s: no validators, auto-approved", stage_node.id)
            return GateResult(
                approved=True,
                mode=GateMode.AUTO,
                reason="No validators configured, auto-approved",
            )

        validator = self._validator
        results = await validator.validate_all(stage_node.validators)
        all_passed = all(r.passed for r in results)
        failures = [r.message for r in results if not r.passed]

        return GateResult(
            approved=all_passed,
            mode=GateMode.AUTO,
            reason="All validators passed" if all_passed else f"Failed: {failures}",
            stage_result=StageResult(
                stage_id=stage_node.id,
                status=StageStatus.GATE_APPROVED if all_passed else StageStatus.GATE_REJECTED,
                error=None if all_passed else "; ".join(failures),
            ),
        )

    async def _process_threshold(
        self,
        stage_node: StageNode,
        state: WorkflowState,
        stage_result: StageResult,
    ) -> GateResult:
        """Threshold 审批模式。

        LLM-as-judge 评分，达到阈值自动 approve。
        分数提取策略：从 stage_result 的输出中提取评分。
        """
        threshold = stage_node.gate_threshold
        score = self._extract_score_from_result(stage_result)

        approved = score >= threshold
        return GateResult(
            approved=approved,
            mode=GateMode.THRESHOLD,
            score=score,
            reason=f"Score {score:.2f} {'>=' if approved else '<'} threshold {threshold}",
            stage_result=StageResult(
                stage_id=stage_node.id,
                status=StageStatus.GATE_APPROVED if approved else StageStatus.GATE_REJECTED,
            ),
        )

    def _extract_score_from_result(self, result: StageResult) -> float:
        """从阶段结果中提取评分。"""
        import re

        if result.error:
            # 尝试从错误信息中提取评分
            match = re.search(r"(?:score|评分)[:\s]*([0-9]*\.?[0-9]+)", result.error, re.IGNORECASE)
            if match:
                try:
                    return float(match.group(1))
                except ValueError:
                    pass
        return 0.0

    async def approve_manual(self, stage_id: int, state: WorkflowState) -> StageResult:
        """手动批准 GATE 阶段。"""
        return StageResult(
            stage_id=stage_id,
            status=StageStatus.GATE_APPROVED,
        )

    async def reject_manual(
        self, stage_id: int, state: WorkflowState, reason: str = ""
    ) -> StageResult:
        """手动拒绝 GATE 阶段。"""
        return StageResult(
            stage_id=stage_id,
            status=StageStatus.GATE_REJECTED,
            error=reason or "Manually rejected",
        )
