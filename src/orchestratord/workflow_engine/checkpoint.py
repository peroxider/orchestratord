"""检查点持久化与恢复。

工作流级检查点持久化，支持从任意阶段恢复执行。
复用 ARC 原子写入模式（temp file + rename）。
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .decision_handler import DecisionHistory
from .errors import CheckpointError, ResumeError
from .workflow_state import StageResult, StageStatus, WorkflowState

logger = logging.getLogger(__name__)


CHECKPOINT_SCHEMA_VERSION = 1


@dataclass
class Checkpoint:
    """工作流检查点。"""

    workflow_name: str
    workflow_version: str
    current_stage: int
    completed_stages: list[int] = field(default_factory=list)
    stage_results: dict[int, dict[str, Any]] = field(default_factory=dict)
    decision_history: list[dict[str, Any]] = field(default_factory=list)
    cost_accumulated_usd: float = 0.0
    started_at: str = ""
    last_checkpoint: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    schema_version: int = CHECKPOINT_SCHEMA_VERSION
    # 扩展状态上下文
    workflow_state_metadata: dict[str, Any] = field(default_factory=dict)
    rollback_events: list[dict[str, Any]] = field(default_factory=list)
    issue_context: dict[str, Any] | None = None
    finished_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "workflow_name": self.workflow_name,
            "workflow_version": self.workflow_version,
            "current_stage": self.current_stage,
            "completed_stages": self.completed_stages,
            "stage_results": {
                str(k): {
                    "status": v.get("status", "unknown"),
                    "outputs": v.get("outputs", []),
                    "artifacts": v.get("artifacts", {}),
                    "error": v.get("error"),
                    "cost_usd": v.get("cost_usd", 0.0),
                    "duration_seconds": v.get("duration_seconds", 0.0),
                    "timestamp": v.get("timestamp", ""),
                }
                for k, v in self.stage_results.items()
            },
            "decision_history": self.decision_history,
            "cost_accumulated_usd": self.cost_accumulated_usd,
            "started_at": self.started_at,
            "last_checkpoint": self.last_checkpoint,
            "metadata": self.metadata,
            "schema_version": self.schema_version,
            "workflow_state_metadata": self.workflow_state_metadata,
            "rollback_events": self.rollback_events,
            "issue_context": self.issue_context,
            "finished_at": self.finished_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Checkpoint":
        schema_version = int(data.get("schema_version", 1))
        if schema_version > CHECKPOINT_SCHEMA_VERSION:
            raise CheckpointError(
                f"Unsupported checkpoint schema version: {schema_version} "
                f"(expected <= {CHECKPOINT_SCHEMA_VERSION})"
            )

        stage_results = {}
        for k, v in data.get("stage_results", {}).items():
            stage_results[int(k)] = v

        issue_context = data.get("issue_context")
        # 避免原始 Issue 对象被序列化后无法简单恢复；from_dict 保留字典形式
        if issue_context is not None and not isinstance(issue_context, dict):
            issue_context = {"_raw": issue_context}

        return cls(
            workflow_name=data.get("workflow_name", ""),
            workflow_version=str(data.get("workflow_version", "1.0")),
            current_stage=int(data.get("current_stage", 0)),
            completed_stages=[int(s) for s in data.get("completed_stages", [])],
            stage_results=stage_results,
            decision_history=data.get("decision_history", []),
            cost_accumulated_usd=float(data.get("cost_accumulated_usd", 0.0)),
            started_at=data.get("started_at", ""),
            last_checkpoint=data.get("last_checkpoint", ""),
            metadata=data.get("metadata", {}),
            schema_version=schema_version,
            workflow_state_metadata=data.get("workflow_state_metadata", {}),
            rollback_events=data.get("rollback_events", []),
            issue_context=issue_context,
            finished_at=data.get("finished_at"),
        )


class CheckpointManager:
    """检查点管理器。

    复用 ARC 原子写入模式（temp file + rename），确保写入不损坏已有检查点。
    """

    def __init__(self, run_dir: str | Path) -> None:
        self._run_dir = Path(run_dir)
        self._run_dir.mkdir(parents=True, exist_ok=True)
        self._checkpoint_path = self._run_dir / "checkpoint.json"

    def save(
        self,
        state: WorkflowState,
        decision_history: list[dict[str, Any]] | None = None,
    ) -> Checkpoint:
        """保存检查点。

        使用原子写入：先写 temp 文件，再 rename。
        """
        checkpoint = Checkpoint(
            workflow_name=state.workflow_name,
            workflow_version=state.workflow_version,
            current_stage=state.current_stage,
            completed_stages=list(state.completed_stages),
            stage_results={
                sid: {
                    "status": sr.status.value,
                    "outputs": sr.outputs,
                    "artifacts": sr.artifacts,
                    "error": sr.error,
                    "cost_usd": sr.cost_usd,
                    "duration_seconds": sr.duration_seconds,
                    "timestamp": sr.timestamp,
                }
                for sid, sr in state.stage_results.items()
            },
            decision_history=decision_history or [],
            cost_accumulated_usd=state.cost_accumulated_usd,
            started_at=state.started_at,
            last_checkpoint=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            metadata=getattr(state, "metadata", {}),
            workflow_state_metadata=getattr(state, "metadata", {}),
            rollback_events=list(getattr(state, "rollback_events", [])),
            issue_context=_serialize_issue_context(state.issue_context),
            finished_at=state.finished_at,
        )

        try:
            data = checkpoint.to_dict()
            json_text = json.dumps(data, ensure_ascii=False, indent=2, default=str)

            # 原子写入：temp file + rename
            temp_path = self._checkpoint_path.with_suffix(".tmp")
            temp_path.write_text(json_text, encoding="utf-8")
            temp_path.replace(self._checkpoint_path)

            logger.info(
                "Checkpoint saved: stage %s, %s stages completed",
                checkpoint.current_stage,
                len(checkpoint.completed_stages),
            )
        except Exception as exc:
            raise CheckpointError(f"Failed to save checkpoint: {exc}") from exc

        return checkpoint

    def load(self) -> Checkpoint:
        """加载检查点。"""
        if not self._checkpoint_path.exists():
            raise CheckpointError(f"Checkpoint file not found: {self._checkpoint_path}")

        try:
            text = self._checkpoint_path.read_text(encoding="utf-8")
            data = json.loads(text)
            return Checkpoint.from_dict(data)
        except json.JSONDecodeError as exc:
            raise CheckpointError(f"Invalid checkpoint JSON: {exc}") from exc
        except Exception as exc:
            raise CheckpointError(f"Failed to load checkpoint: {exc}") from exc

    def exists(self) -> bool:
        """检查是否存在检查点文件。"""
        return self._checkpoint_path.exists()

    def restore_state(self, checkpoint: Checkpoint) -> WorkflowState:
        """从检查点恢复 WorkflowState。"""
        state = WorkflowState(
            workflow_name=checkpoint.workflow_name,
            workflow_version=checkpoint.workflow_version,
        )
        state.current_stage = checkpoint.current_stage
        state.completed_stages = list(checkpoint.completed_stages)
        state.cost_accumulated_usd = checkpoint.cost_accumulated_usd
        state.started_at = checkpoint.started_at

        state.workflow_state_metadata = dict(checkpoint.workflow_state_metadata)
        state.rollback_events = list(checkpoint.rollback_events)
        state.issue_context = checkpoint.issue_context
        state.finished_at = checkpoint.finished_at
        state.decision_history = DecisionHistory.from_dict_list(checkpoint.decision_history)
        # 兼容旧检查点：workflow_state_metadata 与 metadata 同义
        if checkpoint.workflow_state_metadata:
            state.metadata = dict(checkpoint.workflow_state_metadata)

        for sid, sr_data in checkpoint.stage_results.items():
            status_str = sr_data.get("status", "completed")
            try:
                status = StageStatus(status_str)
            except ValueError:
                status = StageStatus.COMPLETED

            result = StageResult(
                stage_id=sid,
                status=status,
                outputs=sr_data.get("outputs", []),
                artifacts=sr_data.get("artifacts", {}),
                error=sr_data.get("error"),
                cost_usd=sr_data.get("cost_usd", 0.0),
                duration_seconds=sr_data.get("duration_seconds", 0.0),
                timestamp=sr_data.get("timestamp", ""),
            )
            state.stage_results[sid] = result
            state.stage_statuses[sid] = status

        return state

    def delete(self) -> None:
        """删除检查点文件。"""
        try:
            self._checkpoint_path.unlink(missing_ok=True)
        except OSError:
            pass


def _serialize_issue_context(issue_context: dict[str, Any] | None) -> dict[str, Any] | None:
    """序列化 issue_context，过滤掉不可序列化的原始对象引用。

    原始 Issue 对象保留在内存中使用，检查点中仅持久化可序列化字段。
    """
    if issue_context is None:
        return None
    if not isinstance(issue_context, dict):
        return None
    # 保留除原始对象引用外的所有字段
    return {k: v for k, v in issue_context.items() if not k.startswith("_")}


class WorkflowResumer:
    """工作流恢复执行器。

    从检查点恢复工作流执行。
    """

    def __init__(self, checkpoint_manager: CheckpointManager) -> None:
        self._checkpoint_manager = checkpoint_manager

    async def resume(self, engine: Any) -> Any:
        """从检查点恢复执行。

        Args:
            engine: DeclarativeWorkflowEngine 实例

        Returns:
            WorkflowResult: 执行结果。
        """
        if not self._checkpoint_manager.exists():
            raise ResumeError("No checkpoint found to resume from")

        checkpoint = self._checkpoint_manager.load()
        logger.info(
            "Resuming workflow '%s' from stage %s (%s stages completed)",
            checkpoint.workflow_name,
            checkpoint.current_stage,
            len(checkpoint.completed_stages),
        )

        # 恢复状态
        state = self._checkpoint_manager.restore_state(checkpoint)
        engine.state = state
        engine.cost_tracker.load_state(total_usd=checkpoint.cost_accumulated_usd)

        # 从当前阶段继续执行
        return await engine.execute(from_stage=checkpoint.current_stage)


class ArtifactResolver:
    """跨阶段产物路径解析器。

    解析阶段间产物引用，如 ${stage:3:output:goal.md}。
    """

    _PATTERN = r"\$\{stage:(\d+):output:([^}]+)\}"

    @classmethod
    def resolve(cls, path_template: str, state: WorkflowState, workspace_dir: str = "") -> str:
        """解析产物路径模板。

        Args:
            path_template: 包含引用的路径模板
            state: 工作流状态
            workspace_dir: 工作区根目录

        Returns:
            解析后的路径。
        """
        import re

        def _replace(match: re.Match) -> str:
            stage_id = int(match.group(1))
            artifact_name = match.group(2)
            result = state.get_stage_result(stage_id)
            if result and artifact_name in result.artifacts:
                return result.artifacts[artifact_name]
            if workspace_dir:
                return str(Path(workspace_dir) / f"stage_{stage_id:02d}" / artifact_name)
            return match.group(0)

        return re.sub(cls._PATTERN, _replace, path_template)
