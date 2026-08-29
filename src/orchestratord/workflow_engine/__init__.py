"""声明式工作流引擎 — 解释执行 workflow.yaml。

子模块:
- engine: DeclarativeWorkflowEngine 核心执行循环
- workflow_state: 工作流运行时状态
- event_bus: 事件总线 + State Journal 写入
- cost: CostTracker + CostBudget
- errors: 异常类型定义
- stage_runner: StageRunner 适配器
- gate_handler: GATE 门禁处理器
- gate_rollback: GATE 回滚处理器
- decision_handler: DECISION 决策处理器
- rollback: 阶段回滚管理器
- validators: 阶段契约验证器
- checkpoint: 检查点持久化
- observability: 工作流可观测性集成
"""

from __future__ import annotations

from .checkpoint import ArtifactResolver, Checkpoint, CheckpointManager, WorkflowResumer
from .cost import CostBudget, CostTracker
from .decision_handler import DecisionHandler, DecisionHistory, DecisionResult
from .engine import DeclarativeWorkflowEngine, EngineConfig, WorkflowResult, WorkflowSchema
from .errors import (
    CheckpointError,
    CostExceededError,
    RollbackError,
    StageFailureError,
    StageTimeoutError,
    ValidationError,
    WorkflowEngineError,
    WorkflowSchemaError,
)
from .event_bus import EventBus
from .gate_handler import GateHandler, GateMode, GateResult
from .gate_rollback import GateRollbackHandler, GateRollbackResult
from .observability import WorkflowObservability, WorkflowProgressSink
from .audit import WorkflowAuditEvent, WorkflowAuditWriter
from .rollback import RollbackManager, RollbackTarget, StageSnapshot
from .stage_runner import DecisionRunResult, GateRunResult, StageRunResult, StageRunner
from .validators import ContractValidator, ValidationResult
from .workflow_state import (
    StageKind,
    StageNode,
    StageResult,
    StageStatus,
    WorkflowState,
)

__all__ = [
    # Engine
    "DeclarativeWorkflowEngine",
    "EngineConfig",
    "WorkflowResult",
    "WorkflowSchema",
    # State
    "WorkflowState",
    "StageNode",
    "StageResult",
    "StageStatus",
    "StageKind",
    # Events
    "EventBus",
    # Cost
    "CostTracker",
    "CostBudget",
    # Errors
    "WorkflowEngineError",
    "WorkflowSchemaError",
    "StageTimeoutError",
    "StageFailureError",
    "CostExceededError",
    "ValidationError",
    "CheckpointError",
    "RollbackError",
    # StageRunner
    "StageRunner",
    "StageRunResult",
    "GateRunResult",
    "DecisionRunResult",
    # GATE
    "GateHandler",
    "GateMode",
    "GateResult",
    "GateRollbackHandler",
    "GateRollbackResult",
    # DECISION
    "DecisionHandler",
    "DecisionHistory",
    "DecisionResult",
    # Rollback
    "RollbackManager",
    "StageSnapshot",
    "RollbackTarget",
    # Validators
    "ContractValidator",
    "ValidationResult",
    # Checkpoint
    "Checkpoint",
    "CheckpointManager",
    "WorkflowResumer",
    "ArtifactResolver",
    # Observability
    "WorkflowObservability",
    "WorkflowProgressSink",
    "WorkflowAuditEvent",
    "WorkflowAuditWriter",
]
