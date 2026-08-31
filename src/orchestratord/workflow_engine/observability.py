"""工作流可观测性集成。

将工作流执行事件集成到编排器的可视化和审计体系。
集成点:
- State Journal NDJSON 事件写入
- WorkflowProgressSink 进度报告
- 工作流级审计事件
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TYPE_CHECKING

from ..state_journal import StateJournalWriter

if TYPE_CHECKING:
    from ..sinks.progress import ProgressSink
    from .workflow_state import WorkflowState

logger = logging.getLogger(__name__)


# ── 工作流可观测性写入器 ───────────────────────────────────────────


class WorkflowObservability:
    """工作流可观测性集成。

    将工作流级事件写入 State Journal，供 Visualizer 消费。
    """

    def __init__(
        self,
        journal: StateJournalWriter | None = None,
        tool_events_path: str | None = None,
    ) -> None:
        self._journal = journal
        self._tool_events_path = tool_events_path

    def write_stage_start(
        self, stage_id: int, stage_name: str, phase: str = "", **kwargs: Any
    ) -> None:
        """写入 stage_start 事件。"""
        self._emit(
            "workflow_stage_start",
            {
                "stage_id": stage_id,
                "stage_name": stage_name,
                "phase": phase,
                **kwargs,
            },
        )

    def write_stage_complete(
        self,
        stage_id: int,
        stage_name: str,
        cost: float = 0.0,
        duration: float = 0.0,
        **kwargs: Any,
    ) -> None:
        """写入 stage_complete 事件。"""
        self._emit(
            "workflow_stage_complete",
            {
                "stage_id": stage_id,
                "stage_name": stage_name,
                "cost_usd": cost,
                "duration_seconds": duration,
                **kwargs,
            },
        )

    def write_stage_failed(
        self, stage_id: int, stage_name: str, error: str = "", **kwargs: Any
    ) -> None:
        """写入 stage_failed 事件。"""
        self._emit(
            "workflow_stage_failed",
            {
                "stage_id": stage_id,
                "stage_name": stage_name,
                "error": error,
                **kwargs,
            },
        )

    def write_gate_request(
        self, stage_id: int, stage_name: str, mode: str = "", **kwargs: Any
    ) -> None:
        """写入 gate_request 事件。"""
        self._emit(
            "workflow_gate_request",
            {
                "stage_id": stage_id,
                "stage_name": stage_name,
                "gate_mode": mode,
                **kwargs,
            },
        )

    def write_gate_result(
        self, stage_id: int, stage_name: str, approved: bool, reason: str = "", **kwargs: Any
    ) -> None:
        """写入 gate_result 事件。"""
        self._emit(
            "workflow_gate_result",
            {
                "stage_id": stage_id,
                "stage_name": stage_name,
                "approved": approved,
                "reason": reason,
                **kwargs,
            },
        )

    def write_decision(
        self, stage_id: int, outcome: str, next_stage: int | None = None, **kwargs: Any
    ) -> None:
        """写入 decision 事件。"""
        self._emit(
            "workflow_decision",
            {
                "stage_id": stage_id,
                "outcome": outcome,
                "next_stage": next_stage,
                **kwargs,
            },
        )

    def write_workflow_complete(
        self,
        total_cost: float,
        total_duration: float,
        completed_stages: int,
        total_stages: int,
        **kwargs: Any,
    ) -> None:
        """写入 workflow_complete 事件。"""
        self._emit(
            "workflow_complete",
            {
                "total_cost_usd": total_cost,
                "total_duration_seconds": total_duration,
                "completed_stages": completed_stages,
                "total_stages": total_stages,
                **kwargs,
            },
        )

    def write_workflow_error(self, error: str, stage_id: int | None = None, **kwargs: Any) -> None:
        """写入 workflow_error 事件。"""
        self._emit(
            "workflow_error",
            {
                "error": error,
                "stage_id": stage_id,
                **kwargs,
            },
        )

    def write_cost_event(
        self, total_usd: float, stage_usd: float, budget_max: float, **kwargs: Any
    ) -> None:
        """写入成本追踪事件。"""
        self._emit(
            "workflow_cost",
            {
                "total_usd": total_usd,
                "stage_usd": stage_usd,
                "budget_max": budget_max,
                "usage_pct": round((total_usd / budget_max * 100), 1) if budget_max > 0 else 0,
                **kwargs,
            },
        )

    def _emit(self, event_type: str, data: dict[str, Any]) -> None:
        """发射事件到 State Journal 和审计日志。"""
        event = {
            "type": event_type,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            **data,
        }

        # 写入 State Journal
        if self._journal is not None:
            try:
                self._journal.write_event(event)
            except Exception as exc:
                logger.debug("Observability journal write failed: %s", exc)

        # 写入审计日志
        if self._tool_events_path is not None:
            try:
                self._append_audit_event(event)
            except Exception as exc:
                logger.debug("Audit event write failed: %s", exc)

    def _append_audit_event(self, event: dict[str, Any]) -> None:
        """追加审计事件到 tool-events NDJSON。"""
        if not self._tool_events_path:
            return
        audit_path = Path(self._tool_events_path)
        audit_path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(event, ensure_ascii=False, default=str) + "\n"
        with open(audit_path, "a", encoding="utf-8") as f:
            f.write(line)


# ── WorkflowProgressSink ─────────────────────────────────────────────


@dataclass
class WorkflowProgressSink:
    """工作流进度报告器。

    实现 ProgressSink 协议，报告阶段完成百分比。
    复用 ProgressSink 协议。
    """

    workflow_name: str = ""
    task_id: str = ""
    total_stages: int = 0
    completed_stages: int = 0
    current_stage: str = ""
    _progress_sinks: list[Any] = field(default_factory=list)

    def add_sink(self, sink: Any) -> None:
        """添加 ProgressSink。"""
        self._progress_sinks.append(sink)

    def on_stage_start(self, stage_id: int, stage_name: str, phase: str = "") -> None:
        """阶段开始。"""
        self.current_stage = f"{stage_name}"

    def on_stage_complete(
        self, stage_id: int, stage_name: str = "", cost: float = 0.0, duration: float = 0.0
    ) -> None:
        """阶段完成时更新进度。"""
        self.completed_stages += 1
        progress = (
            (self.completed_stages / self.total_stages * 100) if self.total_stages > 0 else None
        )

        for sink in self._progress_sinks:
            try:
                if hasattr(sink, "on_phase_complete"):
                    from orchestratord.events.agent_events import PhaseComplete

                    event = PhaseComplete(
                        phase=self.completed_stages,
                        progress=progress,
                        message=f"Stage {stage_id}: {stage_name} completed",
                    )
                    sink.on_phase_complete(event, None)
            except Exception as exc:
                logger.debug("Progress sink update failed: %s", exc)

    def on_stage_failed(self, stage_id: int, error: str) -> None:
        """阶段失败。"""
        self.current_stage = f"FAILED: stage {stage_id}"

    def on_workflow_complete(self, total_cost: float = 0.0, total_duration: float = 0.0) -> None:
        """工作流完成。"""
        for sink in self._progress_sinks:
            try:
                if hasattr(sink, "on_session_complete"):
                    from orchestratord.events.agent_events import SessionComplete

                    event = SessionComplete(reason="success")
                    sink.on_session_complete(event, None)
            except Exception as exc:
                logger.debug("Progress sink complete failed: %s", exc)

    def snapshot(self) -> dict[str, Any]:
        """获取当前进度快照。"""
        return {
            "workflow_name": self.workflow_name,
            "total_stages": self.total_stages,
            "completed_stages": self.completed_stages,
            "current_stage": self.current_stage,
            "progress_pct": self.progress_pct,
        }

    @property
    def progress_pct(self) -> float | None:
        if self.total_stages == 0:
            return None
        return (self.completed_stages / self.total_stages) * 100.0
