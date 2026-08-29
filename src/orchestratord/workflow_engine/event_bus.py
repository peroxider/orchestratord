"""工作流级事件总线 + State Journal 写入。

事件类型:
- workflow_start / workflow_complete / workflow_error
- stage_start / stage_complete / stage_failed / stage_skipped
- gate_request / gate_approved / gate_rejected
- decision_evaluated
- cost_warning
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from ..state_journal import StateJournalWriter

logger = logging.getLogger(__name__)

# 事件处理器类型
EventHandler = Callable[[str, dict[str, Any]], None]


class EventBus:
    """工作流事件总线。

    支持同步事件发射 + State Journal 持久化。
    """

    def __init__(self, journal: StateJournalWriter | None = None) -> None:
        self._handlers: dict[str, list[EventHandler]] = {}
        self._journal = journal
        self._event_log: list[dict[str, Any]] = []

    def on(self, event_type: str, handler: EventHandler) -> None:
        """注册事件处理器。"""
        if event_type not in self._handlers:
            self._handlers[event_type] = []
        self._handlers[event_type].append(handler)

    def emit(self, event_type: str, **kwargs: Any) -> None:
        """发射事件。

        1. 写入 State Journal（如果配置了）
        2. 通知所有注册的处理器
        3. 记录到内存日志
        """
        event_data = {"type": event_type, **kwargs}

        # 写入 State Journal
        if self._journal is not None:
            try:
                self._journal.write_event(event_data)
            except Exception as exc:
                logger.debug("Failed to write event to journal: %s", exc)

        # 通知处理器
        for handler in self._handlers.get(event_type, []):
            try:
                handler(event_type, event_data)
            except Exception as exc:
                logger.warning("Event handler for %s failed: %s", event_type, exc)

        # 记录到内存日志
        self._event_log.append(event_data)

    # ── 便捷方法 ──────────────────────────────────────────────

    def emit_workflow_start(self, workflow_name: str, total_stages: int, **kwargs: Any) -> None:
        self.emit(
            "workflow_start", workflow_name=workflow_name, total_stages=total_stages, **kwargs
        )

    def emit_workflow_complete(
        self, total_cost: float, total_duration: float, **kwargs: Any
    ) -> None:
        self.emit(
            "workflow_complete", total_cost=total_cost, total_duration=total_duration, **kwargs
        )

    def emit_workflow_error(self, error: str, stage_id: int | None = None, **kwargs: Any) -> None:
        self.emit("workflow_error", error=error, stage_id=stage_id, **kwargs)

    def emit_stage_start(
        self, stage_id: int, stage_name: str, phase: str = "", **kwargs: Any
    ) -> None:
        self.emit("stage_start", stage_id=stage_id, stage_name=stage_name, phase=phase, **kwargs)

    def emit_stage_complete(
        self,
        stage_id: int,
        stage_name: str,
        cost: float = 0.0,
        duration: float = 0.0,
        **kwargs: Any,
    ) -> None:
        self.emit(
            "stage_complete",
            stage_id=stage_id,
            stage_name=stage_name,
            cost=cost,
            duration=duration,
            **kwargs,
        )

    def emit_stage_failed(
        self, stage_id: int, stage_name: str, error: str = "", **kwargs: Any
    ) -> None:
        self.emit("stage_failed", stage_id=stage_id, stage_name=stage_name, error=error, **kwargs)

    def emit_stage_skipped(
        self, stage_id: int, stage_name: str, reason: str = "", **kwargs: Any
    ) -> None:
        self.emit(
            "stage_skipped", stage_id=stage_id, stage_name=stage_name, reason=reason, **kwargs
        )

    def emit_gate_request(
        self, stage_id: int, stage_name: str, mode: str = "", **kwargs: Any
    ) -> None:
        self.emit("gate_request", stage_id=stage_id, stage_name=stage_name, mode=mode, **kwargs)

    def emit_gate_approved(self, stage_id: int, stage_name: str, **kwargs: Any) -> None:
        self.emit("gate_approved", stage_id=stage_id, stage_name=stage_name, **kwargs)

    def emit_gate_rejected(
        self, stage_id: int, stage_name: str, reason: str = "", **kwargs: Any
    ) -> None:
        self.emit(
            "gate_rejected", stage_id=stage_id, stage_name=stage_name, reason=reason, **kwargs
        )

    def emit_decision(
        self, stage_id: int, outcome: str, next_stage: int | None = None, **kwargs: Any
    ) -> None:
        self.emit(
            "decision_evaluated",
            stage_id=stage_id,
            outcome=outcome,
            next_stage=next_stage,
            **kwargs,
        )

    def emit_cost_warning(self, message: str, **kwargs: Any) -> None:
        self.emit("cost_warning", message=message, **kwargs)

    @property
    def event_log(self) -> list[dict[str, Any]]:
        return list(self._event_log)
