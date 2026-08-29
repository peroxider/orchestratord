"""声明式工作流引擎核心。

读取 workflow.yaml，按 DAG 顺序调度 Agent，管理 GATE/DECISION/回环，
提供工作流级错误恢复和成本追踪。
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .cost import CostBudget, CostTracker
from .decision_handler import DecisionHandler
from .errors import (
    ConvergenceError,
    CostExceededError,
    DecisionExhaustedError,
    RollbackError,
    StageFailureError,
    StageTimeoutError,
    WorkflowEngineError,
    WorkflowSchemaError,
)
from .checkpoint import CheckpointManager
from .event_bus import EventBus
from .gate_handler import GateHandler
from .rollback import RollbackManager
from .gate_rollback import GateRollbackHandler
from .validators import ContractValidator
from .workflow_state import (
    StageKind,
    StageNode,
    StageResult,
    StageStatus,
    WorkflowState,
)

logger = logging.getLogger(__name__)


# ── Workflow Schema ──────────────────────────────────────────────────


@dataclass
class EngineConfig:
    """引擎运行时配置。"""

    cost_budget: CostBudget = field(default_factory=CostBudget)
    max_concurrent_stages: int = 1
    default_timeout_seconds: int = 600
    workspace_dir: str = ""
    run_dir: str = ""
    run_id: str = ""
    enable_snapshots: bool = False  # 是否启用阶段快照（用于回滚）
    llm_client: Any | None = None  # 供 llm_judge 验证器使用


@dataclass
class WorkflowSchema:
    """workflow.yaml 的解析结果。"""

    name: str
    version: str = "1.0"
    description: str = ""
    stages: list[StageNode] = field(default_factory=list)
    config: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "WorkflowSchema":
        """从字典构建 WorkflowSchema。"""
        name = data.get("name", "unnamed")
        version = str(data.get("version", "1.0"))
        description = data.get("description", "")

        stages_raw = data.get("stages", [])
        if not isinstance(stages_raw, list):
            raise WorkflowSchemaError("workflow.yaml: 'stages' must be a list")

        stages = [_parse_stage_node(i, s) for i, s in enumerate(stages_raw)]
        # 检查重复 stage_id
        seen_ids: set[int] = set()
        for s in stages:
            if s.id in seen_ids:
                raise WorkflowSchemaError(f"workflow.yaml: duplicate stage id '{s.id}'")
            seen_ids.add(s.id)
        config = data.get("config", {})
        if not isinstance(config, dict):
            config = {}

        return cls(
            name=name,
            version=version,
            description=description,
            stages=stages,
            config=config,
        )

    @classmethod
    def from_yaml(cls, path: str | Path) -> "WorkflowSchema":
        """从 YAML 文件加载 WorkflowSchema。"""
        path = Path(path)
        if not path.exists():
            raise WorkflowSchemaError(f"Workflow file not found: {path}")
        content = path.read_text(encoding="utf-8")
        try:
            data = yaml.safe_load(content)
        except yaml.YAMLError as exc:
            raise WorkflowSchemaError(f"Invalid YAML: {exc}") from exc
        if not isinstance(data, dict):
            raise WorkflowSchemaError("workflow.yaml root must be a mapping")
        return cls.from_dict(data)

    def get_stage(self, stage_id: int) -> StageNode | None:
        for s in self.stages:
            if s.id == stage_id:
                return s
        return None

    def build_dag_order(self) -> list[int]:
        """按 DAG 拓扑排序返回阶段 ID 列表。"""
        stage_ids = {s.id for s in self.stages}
        in_degree: dict[int, int] = {s.id: len(s.depends_on) for s in self.stages}
        adj: dict[int, list[int]] = {s.id: [] for s in self.stages}

        for s in self.stages:
            for dep in s.depends_on:
                if dep in adj:
                    adj[dep].append(s.id)

        # Kahn 算法
        queue = [sid for sid, deg in in_degree.items() if deg == 0]
        order: list[int] = []

        while queue:
            node = queue.pop(0)
            order.append(node)
            for neighbor in adj.get(node, []):
                in_degree[neighbor] -= 1
                if in_degree[neighbor] == 0:
                    queue.append(neighbor)

        if len(order) != len(stage_ids):
            raise WorkflowSchemaError("Workflow DAG contains a cycle")

        return order


def _parse_stage_node(index: int, raw: dict[str, Any]) -> StageNode:
    """解析单个阶段节点。"""
    kind_str = str(raw.get("kind", "agent")).lower()
    try:
        kind = StageKind(kind_str)
    except ValueError:
        raise WorkflowSchemaError(f"Stage[{index}]: unknown kind '{kind_str}'")

    return StageNode(
        id=raw.get("id", index + 1),
        name=raw.get("name", f"stage-{index + 1}"),
        kind=kind,
        phase=raw.get("phase", ""),
        prompt=raw.get("prompt", ""),
        depends_on=_normalize_int_list(raw.get("depends_on", [])),
        agent_config=raw.get("agent_config", {}),
        validators=raw.get("validators", []),
        gate_mode=raw.get("gate_mode", "manual"),
        gate_threshold=float(raw.get("gate_threshold", 0.8)),
        gate_rollback_to=raw.get("gate_rollback_to"),
        decision_outcomes=raw.get("decision_outcomes", {}),
        timeout_seconds=int(raw.get("timeout_seconds", 0)),
        max_retries=int(raw.get("max_retries", 0)),
        on_error=raw.get("on_error", "fail"),
    )


def _normalize_int_list(value: Any) -> list[int]:
    if isinstance(value, list):
        return [int(v) for v in value]
    return []


# ── DeclarativeWorkflowEngine ────────────────────────────────────────


@dataclass
class WorkflowResult:
    """工作流执行结果。"""

    success: bool
    workflow_name: str
    completed_stages: int
    total_stages: int
    total_cost_usd: float
    total_duration_seconds: float
    error: str | None = None
    stage_results: dict[int, StageResult] = field(default_factory=dict)


class DeclarativeWorkflowEngine:
    """声明式工作流引擎 — 解释执行 workflow.yaml。"""

    def __init__(
        self,
        workflow: WorkflowSchema,
        config: EngineConfig | None = None,
    ) -> None:
        self.workflow = workflow
        self.config = config or EngineConfig()
        self.state = WorkflowState(
            workflow_name=workflow.name,
            workflow_version=workflow.version,
        )
        self.cost_tracker = CostTracker(budget=self.config.cost_budget)
        self.event_bus = EventBus()
        self._dag_order: list[int] = []
        self._stage_runner = None  # 延迟注入

        # 阶段输出验证器
        self._validator = ContractValidator(
            workspace_dir=self.config.workspace_dir,
            llm_client=self.config.llm_client,
        )

        # 回滚系统
        self._rollback_manager: RollbackManager | None = None
        self._gate_rollback_handler: GateRollbackHandler | None = None
        self._init_rollback()

        # 检查点系统
        self._checkpoint_manager: CheckpointManager | None = None

        # GATE/DECISION 处理器
        self._gate_handler = GateHandler(
            workspace_dir=self.config.workspace_dir,
            llm_client=self.config.llm_client,
        )
        self._decision_handler = DecisionHandler()
        self._decision_count: dict[int, int] = {}  # 保留兼容；实际计数由 DecisionHandler.history 维护

    def set_stage_runner(self, runner: Any) -> None:
        """注入 StageRunner 适配器。"""
        self._stage_runner = runner

    def _init_rollback(self) -> None:
        """初始化回滚系统。"""
        if self.config.enable_snapshots and self.config.workspace_dir:
            self._rollback_manager = RollbackManager(
                workspace_dir=self.config.workspace_dir,
            )
            self._gate_rollback_handler = GateRollbackHandler(
                rollback_manager=self._rollback_manager,
                workspace_dir=self.config.workspace_dir,
            )

    def set_checkpoint_manager(self, checkpoint_manager: CheckpointManager) -> None:
        """注入检查点管理器。"""
        self._checkpoint_manager = checkpoint_manager

    def _effective_timeout(self, stage: StageNode) -> int:
        """解析阶段有效超时时间。

        stage.timeout_seconds == 0 表示未显式配置，使用引擎默认值。
        """
        return stage.timeout_seconds or self.config.default_timeout_seconds

    def _save_checkpoint(self, current_stage_id: int) -> None:
        """保存检查点。"""
        if self._checkpoint_manager is None:
            return
        self.state.current_stage = current_stage_id
        try:
            self._checkpoint_manager.save(
                self.state,
                decision_history=self._decision_handler.history.to_dict(),
            )
        except Exception:
            logger.debug("Failed to save checkpoint at stage %s", current_stage_id, exc_info=True)

    async def execute(self, from_stage: int | None = None) -> WorkflowResult:
        """执行工作流。

        Args:
            from_stage: 从指定阶段恢复执行（用于检查点恢复）。

        Returns:
            WorkflowResult: 执行结果。
        """
        start_time = time.time()

        try:
            self._dag_order = self.workflow.build_dag_order()
        except WorkflowSchemaError as exc:
            return WorkflowResult(
                success=False,
                workflow_name=self.workflow.name,
                completed_stages=0,
                total_stages=len(self.workflow.stages),
                total_cost_usd=0.0,
                total_duration_seconds=0.0,
                error=str(exc),
            )

        # 初始化所有阶段状态
        for stage in self.workflow.stages:
            self.state.stage_statuses[stage.id] = StageStatus.PENDING

        self.event_bus.emit_workflow_start(
            workflow_name=self.workflow.name,
            total_stages=len(self.workflow.stages),
        )

        # 确定起始位置
        start_index = 0
        if from_stage is not None:
            for i, sid in enumerate(self._dag_order):
                if sid == from_stage:
                    start_index = i
                    break
            # 标记之前阶段为已完成
            for i in range(start_index):
                sid = self._dag_order[i]
                if sid not in self.state.completed_stages:
                    self.state.completed_stages.append(sid)
                    self.state.stage_statuses[sid] = StageStatus.COMPLETED

        # 执行循环
        error_msg: str | None = None
        idx = start_index
        while idx < len(self._dag_order):
            stage_id = self._dag_order[idx]
            stage = self.workflow.get_stage(stage_id)
            if stage is None:
                idx += 1
                continue

            # 检查依赖是否完成
            if not self._dependencies_satisfied(stage):
                self.event_bus.emit_stage_skipped(
                    stage_id=stage.id,
                    stage_name=stage.name,
                    reason="dependencies not satisfied",
                )
                self.state.stage_statuses[stage.id] = StageStatus.SKIPPED
                idx += 1
                continue

            try:
                result = await self._execute_stage(stage)
                self.state.mark_stage_completed(stage.id, result)

                # 每个阶段完成后保存检查点
                self._save_checkpoint(stage.id)

                self.event_bus.emit_stage_complete(
                    stage_id=stage.id,
                    stage_name=stage.name,
                    cost=result.cost_usd,
                    duration=result.duration_seconds,
                )

                # GATE 拒绝处理
                if stage.is_gate_stage and result.status == StageStatus.GATE_REJECTED:
                    if stage.on_error == "rollback" or stage.gate_rollback_to is not None:
                        error_msg = result.error or f"GATE stage {stage.id} rejected"
                        idx = await self._handle_gate_rejection(stage, result)
                        continue
                    elif stage.on_error == "skip":
                        logger.info("GATE stage %s rejected, skipping (on_error=skip)", stage.id)
                        # Mark as COMPLETED so downstream dependencies are satisfied
                        result.status = StageStatus.COMPLETED
                        idx += 1
                        continue
                    else:
                        # on_error == "fail" (default)
                        error_msg = result.error or f"GATE stage {stage.id} rejected"
                        break

                # DECISION 阶段：计算下一个阶段
                if stage.is_decision_stage and result.decision_next_stage is not None:
                    try:
                        next_idx = self._dag_order.index(result.decision_next_stage)
                        idx = next_idx
                        continue
                    except ValueError:
                        logger.warning(
                            "Decision next_stage %s not in DAG order", result.decision_next_stage
                        )

                idx += 1

            except StageTimeoutError as exc:
                result = self._handle_stage_error(stage, exc, "timeout")
                self.state.mark_stage_failed(stage.id, str(exc))
                error_msg = str(exc)
                if stage.on_error == "fail":
                    break
                idx += 1

            except StageFailureError as exc:
                result = self._handle_stage_error(stage, exc, "failure")
                self.state.mark_stage_failed(stage.id, str(exc))
                error_msg = str(exc)
                if stage.on_error == "fail":
                    break
                elif stage.on_error == "rollback":
                    idx = await self._rollback_to_stage(stage)
                    continue
                idx += 1

            except CostExceededError as exc:
                self.event_bus.emit_workflow_error(error=str(exc), stage_id=stage.id)
                error_msg = str(exc)
                break

            except WorkflowEngineError as exc:
                self.event_bus.emit_workflow_error(error=str(exc), stage_id=stage.id)
                error_msg = str(exc)
                break

        self.state.mark_workflow_finished()
        total_duration = time.time() - start_time

        if error_msg:
            self.event_bus.emit_workflow_error(error=error_msg)
        else:
            self.event_bus.emit_workflow_complete(
                total_cost=self.cost_tracker.total_usd,
                total_duration=total_duration,
            )

        return WorkflowResult(
            success=error_msg is None,
            workflow_name=self.workflow.name,
            completed_stages=self.state.completed_count,
            total_stages=self.state.total_stages,
            total_cost_usd=self.cost_tracker.total_usd,
            total_duration_seconds=total_duration,
            error=error_msg,
            stage_results=dict(self.state.stage_results),
        )

    # ── 阶段执行 ──────────────────────────────────────────────────

    async def _execute_stage(self, stage: StageNode) -> StageResult:
        """执行单个阶段。

        根据阶段类型分发到不同的处理器。
        """
        self.state.mark_stage_running(stage.id)
        self.event_bus.emit_stage_start(
            stage_id=stage.id,
            stage_name=stage.name,
            phase=stage.phase,
        )

        # 保存快照（用于回滚）
        self._save_stage_snapshot(stage)

        stage_start = time.time()

        if stage.is_agent_stage:
            result = await self._run_agent_stage(stage)
        elif stage.is_gate_stage:
            result = await self._run_gate_stage(stage)
        elif stage.is_decision_stage:
            result = await self._run_decision_stage(stage)
        else:
            result = StageResult(
                stage_id=stage.id,
                status=StageStatus.COMPLETED,
            )

        result.duration_seconds = time.time() - stage_start
        return result

    async def _run_agent_stage(self, stage: StageNode) -> StageResult:
        """执行 Agent 阶段。

        通过 StageRunner 适配器调用 AgentRunner。
        """
        self.cost_tracker.reset_stage()

        if self._stage_runner is None:
            raise WorkflowEngineError(
                "StageRunner not injected. Call set_stage_runner() before execute().",
                stage_id=stage.id,
            )

        effective_timeout = self._effective_timeout(stage)
        try:
            run_result = await asyncio.wait_for(
                self._stage_runner.run(stage, self.state),
                timeout=effective_timeout,
            )
        except asyncio.TimeoutError:
            raise StageTimeoutError(
                f"Stage {stage.id} ({stage.name}) timed out after {effective_timeout}s",
                stage_id=stage.id,
            )

        cost = getattr(run_result, "cost_usd", 0.0)
        self.cost_tracker.add(cost)

        # 预算检查
        warnings = self.cost_tracker.check_budget()
        for w in warnings:
            self.event_bus.emit_cost_warning(message=w)

        # 检查 StageRunner 执行结果
        if not getattr(run_result, "success", False):
            error_msg = getattr(run_result, "error", "Stage execution failed")
            raise StageFailureError(
                f"Stage {stage.id} ({stage.name}) failed: {error_msg}",
                stage_id=stage.id,
            )

        # 输出验证
        if stage.validators:
            validation_errors = await self._validate_stage_output(stage, run_result)
            if validation_errors:
                raise StageFailureError(
                    f"Stage {stage.id} validation failed: {validation_errors}",
                    stage_id=stage.id,
                )

        return StageResult(
            stage_id=stage.id,
            status=StageStatus.COMPLETED,
            outputs=getattr(run_result, "outputs", []),
            artifacts=getattr(run_result, "artifacts", {}),
            cost_usd=cost,
        )

    async def _run_gate_stage(self, stage: StageNode) -> StageResult:
        """执行 GATE 阶段。"""
        if self._stage_runner is None:
            raise WorkflowEngineError("StageRunner not injected", stage_id=stage.id)

        self.event_bus.emit_gate_request(
            stage_id=stage.id,
            stage_name=stage.name,
            mode=stage.gate_mode,
        )

        effective_timeout = self._effective_timeout(stage)
        try:
            gate_result = await asyncio.wait_for(
                self._stage_runner.run_gate(stage, self.state),
                timeout=effective_timeout,
            )
        except asyncio.TimeoutError:
            raise StageTimeoutError(
                f"GATE stage {stage.id} timed out",
                stage_id=stage.id,
            )

        approved = getattr(gate_result, "approved", False)
        if approved:
            self.event_bus.emit_gate_approved(
                stage_id=stage.id,
                stage_name=stage.name,
            )
            return StageResult(
                stage_id=stage.id,
                status=StageStatus.GATE_APPROVED,
            )
        else:
            self.event_bus.emit_gate_rejected(
                stage_id=stage.id,
                stage_name=stage.name,
                reason=getattr(gate_result, "reason", "unknown"),
            )
            return StageResult(
                stage_id=stage.id,
                status=StageStatus.GATE_REJECTED,
                error=getattr(gate_result, "reason", "GATE rejected"),
            )

    async def _run_decision_stage(self, stage: StageNode) -> StageResult:
        """执行 DECISION 阶段。

        通过 DecisionHandler 进行回环次数检查和收敛检测。
        """
        if self._stage_runner is None:
            raise WorkflowEngineError("StageRunner not injected", stage_id=stage.id)

        effective_timeout = self._effective_timeout(stage)
        try:
            decision_result = await asyncio.wait_for(
                self._stage_runner.run_decision(stage, self.state),
                timeout=effective_timeout,
            )
        except asyncio.TimeoutError:
            raise StageTimeoutError(
                f"DECISION stage {stage.id} timed out",
                stage_id=stage.id,
            )

        outcome = getattr(decision_result, "outcome", "proceed")
        next_stage = getattr(decision_result, "next_stage", None)

        stage_result = StageResult(
            stage_id=stage.id,
            status=StageStatus.COMPLETED,
            decision_outcome=outcome,
            decision_next_stage=next_stage,
        )

        decision = self._decision_handler.resolve(stage, stage_result)

        if decision.exhausted:
            self.event_bus.emit_decision(
                stage_id=stage.id,
                outcome=outcome,
                next_stage=None,
            )
            raise DecisionExhaustedError(
                decision.reason,
                stage_id=stage.id,
            )

        if decision.converged:
            self.event_bus.emit_decision(
                stage_id=stage.id,
                outcome=outcome,
                next_stage=None,
            )
            raise ConvergenceError(
                decision.reason,
                stage_id=stage.id,
            )

        resolved_next = decision.next_stage or next_stage
        self.event_bus.emit_decision(
            stage_id=stage.id,
            outcome=decision.outcome,
            next_stage=resolved_next,
        )

        return StageResult(
            stage_id=stage.id,
            status=StageStatus.COMPLETED,
            decision_outcome=decision.outcome,
            decision_next_stage=resolved_next,
        )

    async def _validate_stage_output(self, stage: StageNode, run_result: Any) -> list[str]:
        """执行阶段输出验证。

        委托给注入的 ``ContractValidator`` 实例，支持全部 7 种验证器类型。
        """
        results = await self._validator.validate_all(stage.validators)
        return [r.message for r in results if not r.passed]

    # ── 错误处理 ──────────────────────────────────────────────────

    def _handle_stage_error(
        self, stage: StageNode, exc: WorkflowEngineError, error_type: str
    ) -> StageResult:
        """处理阶段错误。"""
        self.event_bus.emit_stage_failed(
            stage_id=stage.id,
            stage_name=stage.name,
            error=str(exc),
        )
        return StageResult(
            stage_id=stage.id,
            status=StageStatus.FAILED,
            error=str(exc),
        )

    async def _rollback_to_stage(self, stage: StageNode) -> int:
        """回滚到指定阶段（使用 RollbackManager）。

        优先级:
        1. 使用 RollbackManager 恢复快照
        2. 降级到依赖阶段回滚
        """
        if self._rollback_manager is not None:
            try:
                target = self._rollback_manager.resolve_rollback_target(stage)
                self._rollback_manager.restore_snapshot(target.stage_id)
                self._rollback_manager.update_state_on_rollback(
                    self.state,
                    target.stage_id,
                    stage.id,
                )
                return self._dag_order.index(target.stage_id)
            except (RollbackError, ValueError) as exc:
                logger.warning("Rollback failed: %s, falling back to simple rollback", exc)

        # 降级：简单回滚到依赖阶段
        if stage.depends_on:
            target = stage.depends_on[0]
        else:
            target = self._dag_order[0] if self._dag_order else 0
        try:
            return self._dag_order.index(target)
        except ValueError:
            return 0

    def _save_stage_snapshot(self, stage: StageNode) -> None:
        """保存阶段执行前快照。"""
        if self._rollback_manager is not None:
            try:
                self._rollback_manager.save_snapshot(stage)
            except Exception as exc:
                logger.debug("Failed to save snapshot for stage %s: %s", stage.id, exc)

    async def _handle_gate_rejection(self, stage: StageNode, result: StageResult) -> int:
        """处理 GATE 拒绝：执行回滚。

        Returns:
            回滚目标在 DAG 中的索引
        """
        if self._gate_rollback_handler is not None:
            try:
                new_idx, gate_result = await self._gate_rollback_handler.handle_gate_rejection(
                    stage=stage,
                    state=self.state,
                    dag_order=self._dag_order,
                    rejection_reason=result.error or "GATE rejected",
                )
                self.event_bus.emit_gate_rejected(
                    stage_id=stage.id,
                    stage_name=stage.name,
                    reason=gate_result.reason,
                )
                return new_idx
            except Exception as exc:
                logger.warning("Gate rollback failed: %s", exc)

        # 降级：简单回滚
        return await self._rollback_to_stage(stage)

    # ── 依赖检查 ──────────────────────────────────────────────────

    def _dependencies_satisfied(self, stage: StageNode) -> bool:
        """检查所有依赖阶段是否已完成。"""
        for dep_id in stage.depends_on:
            if dep_id not in self.state.completed_stages:
                return False
            dep_result = self.state.get_stage_result(dep_id)
            if dep_result is None or dep_result.status not in (
                StageStatus.COMPLETED,
                StageStatus.GATE_APPROVED,
            ):
                return False
        return True
