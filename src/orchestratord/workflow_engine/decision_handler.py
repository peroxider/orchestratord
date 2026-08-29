"""DECISION 决策处理器。

处理工作流中的决策点——多结果分支、回环、收敛检测。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from .errors import ConvergenceError, DecisionExhaustedError
from .workflow_state import StageNode, StageResult, WorkflowState

logger = logging.getLogger(__name__)


@dataclass
class DecisionRecord:
    """单条决策记录。"""

    stage_id: int
    outcome: str
    timestamp: str = ""
    next_stage: int | None = None


@dataclass
class DecisionHistory:
    """决策历史追踪器。"""

    records: list[DecisionRecord] = field(default_factory=list)

    def record(self, stage_id: int, outcome: str, next_stage: int | None = None) -> None:
        import time

        self.records.append(
            DecisionRecord(
                stage_id=stage_id,
                outcome=outcome,
                timestamp=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                next_stage=next_stage,
            )
        )

    def count(self, outcome: str, stage_id: int) -> int:
        """统计特定阶段+结果的次数。"""
        return sum(1 for r in self.records if r.stage_id == stage_id and r.outcome == outcome)

    def is_degenerate(self, outcome: str, stage_id: int, window: int = 5) -> bool:
        """检测退化循环：同一结果在最近 N 次中连续出现。"""
        recent = [r for r in self.records[-window:] if r.stage_id == stage_id]
        if len(recent) < window:
            return False
        return all(r.outcome == outcome for r in recent)

    def to_dict(self) -> list[dict[str, Any]]:
        return [
            {
                "stage": r.stage_id,
                "outcome": r.outcome,
                "timestamp": r.timestamp,
                "next_stage": r.next_stage,
            }
            for r in self.records
        ]

    @classmethod
    def from_dict_list(cls, records: list[dict[str, Any]]) -> "DecisionHistory":
        """从检查点记录列表重建决策历史。"""
        history = cls()
        for r in records:
            history.records.append(
                DecisionRecord(
                    stage_id=int(r.get("stage", 0)),
                    outcome=r.get("outcome", ""),
                    timestamp=r.get("timestamp", ""),
                    next_stage=r.get("next_stage"),
                )
            )
        return history

    def count_by_stage(self, stage_id: int) -> int:
        """统计指定阶段的所有决策记录数。"""
        return sum(1 for r in self.records if r.stage_id == stage_id)

    def counts(self) -> dict[int, int]:
        """返回每个 stage_id 的决策次数映射。"""
        counts: dict[int, int] = {}
        for r in self.records:
            counts[r.stage_id] = counts.get(r.stage_id, 0) + 1
        return counts

@dataclass
class DecisionResult:
    """决策处理结果。"""

    outcome: str
    next_stage: int | None = None
    rollback_to: int | None = None
    exhausted: bool = False
    converged: bool = False
    reason: str = ""


class DecisionHandler:
    """DECISION 决策处理器。

    核心逻辑:
    1. 解析 LLM 输出中的决策结果 (proceed / pivot / refine / ...)
    2. 回环次数检查 (max_times)
    3. 收敛检测 (degenerate loop detection)
    """

    def __init__(self) -> None:
        self._history = DecisionHistory()

    @property
    def history(self) -> DecisionHistory:
        return self._history

    def resolve(
        self,
        node: StageNode,
        result: StageResult,
    ) -> DecisionResult:
        """解析决策结果。

        Args:
            node: 决策阶段节点
            result: 阶段执行结果（包含 LLM 输出的 decision_outcome）

        Returns:
            DecisionResult: 决策结果。
        """
        outcome = result.decision_outcome or "proceed"
        decision_spec = node.decision_outcomes.get(outcome, {})

        # 回环次数检查
        max_times = decision_spec.get("max_times")
        if max_times is not None:
            times = self._history.count(outcome, node.id)
            if times >= max_times:
                exhausted_action = decision_spec.get("on_exhaust", "rollback")
                rollback_to = decision_spec.get(
                    "rollback_to", node.depends_on[0] if node.depends_on else None
                )
                self._history.record(node.id, outcome, None)
                return DecisionResult(
                    outcome=outcome,
                    exhausted=True,
                    rollback_to=rollback_to,
                    reason=f"Max retries ({max_times}) exceeded for outcome '{outcome}'",
                )

        # 收敛检查
        convergence_check = decision_spec.get("convergence_check", False)
        if convergence_check:
            if self._history.is_degenerate(outcome, node.id):
                self._history.record(node.id, outcome, None)
                return DecisionResult(
                    outcome=outcome,
                    converged=True,
                    reason=f"Convergence detected: degenerate loop for outcome '{outcome}'",
                )

        next_stage = decision_spec.get("next")
        rollback_to = decision_spec.get("rollback_to")

        self._history.record(node.id, outcome, next_stage)

        return DecisionResult(
            outcome=outcome,
            next_stage=next_stage,
            rollback_to=rollback_to,
            reason=f"Decision resolved: {outcome} -> next_stage={next_stage}",
        )

    def reset(self) -> None:
        """重置决策历史。"""
        self._history = DecisionHistory()
