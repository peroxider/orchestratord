"""工作流引擎异常类型定义。"""

from __future__ import annotations

from typing import Any


class WorkflowEngineError(Exception):
    """工作流引擎基础异常。"""

    def __init__(
        self, message: str, stage_id: int | None = None, details: dict[str, Any] | None = None
    ):
        super().__init__(message)
        self.stage_id = stage_id
        self.details = details or {}


class StageTimeoutError(WorkflowEngineError):
    """阶段执行超时。"""


class StageFailureError(WorkflowEngineError):
    """阶段执行失败。"""


class CostExceededError(WorkflowEngineError):
    """成本预算超额。"""


class ValidationError(WorkflowEngineError):
    """阶段输出验证失败。"""


class GateRejectedError(WorkflowEngineError):
    """GATE 审批被拒绝。"""


class DecisionExhaustedError(WorkflowEngineError):
    """DECISION 回环次数耗尽。"""


class ConvergenceError(WorkflowEngineError):
    """DECISION 收敛检测触发——退化循环。"""


class CheckpointError(WorkflowEngineError):
    """检查点读写失败。"""


class WorkflowSchemaError(WorkflowEngineError):
    """workflow.yaml 格式错误。"""


class ResumeError(WorkflowEngineError):
    """从检查点恢复执行失败。"""


class RollbackError(WorkflowEngineError):
    """阶段回滚失败。"""
