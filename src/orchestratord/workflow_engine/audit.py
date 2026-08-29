"""Workflow audit writer.

Appends one NDJSON line per workflow lifecycle event to
``~/.cache/orchestratord/workflow-events/{workflow_name}/events.ndjson``
so that workflow executions can be audited after the fact without
importing any orchestrator code.

Design principle: the writer is fire-and-forget. Write failures are
logged at debug level and never propagated — a broken audit log must
not crash the workflow run.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class WorkflowAuditEvent:
    """Typed view over one workflow audit event (NDJSON line)."""

    type: str
    timestamp: str = ""
    workflow: str = ""
    data: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "WorkflowAuditEvent":
        """Build an event from a raw NDJSON dict."""
        data = {k: v for k, v in raw.items() if k not in ("type", "timestamp", "workflow")}
        return cls(
            type=raw.get("type", ""),
            timestamp=raw.get("timestamp", ""),
            workflow=raw.get("workflow", ""),
            data=data,
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialize back to the NDJSON dict shape."""
        out: dict[str, Any] = {"type": self.type}
        if self.timestamp:
            out["timestamp"] = self.timestamp
        if self.workflow:
            out["workflow"] = self.workflow
        out.update(self.data)
        return out


class WorkflowAuditWriter:
    """Append-only NDJSON writer for workflow audit events."""

    def __init__(self, workflow_name: str, events_dir: Path | None = None) -> None:
        self._workflow_name = workflow_name

        if events_dir is None:
            events_dir = Path.home() / ".cache" / "orchestratord" / "workflow-events" / workflow_name
        self._events_dir = Path(events_dir)
        self._events_dir.mkdir(parents=True, exist_ok=True)
        self._events_path = self._events_dir / "events.ndjson"

    @property
    def events_path(self) -> Path:
        return self._events_path

    # ------------------------------------------------------------------
    # Core
    # ------------------------------------------------------------------

    def write_event(self, event: dict[str, Any]) -> None:
        """Append one NDJSON line; injects ``timestamp`` when absent."""
        try:
            if "timestamp" not in event:
                event["timestamp"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            if "workflow" not in event:
                event["workflow"] = self._workflow_name
            line = json.dumps(event, ensure_ascii=False, default=str) + "\n"
            with open(self._events_path, "a", encoding="utf-8") as f:
                f.write(line)
        except Exception as exc:
            logger.debug("workflow audit write failed: %s", exc)

    # ------------------------------------------------------------------
    # Stage lifecycle
    # ------------------------------------------------------------------

    def write_stage_start(self, stage_id: str, stage_name: str, phase: str) -> None:
        self.write_event(
            {
                "type": "stage_start",
                "stage_id": stage_id,
                "stage_name": stage_name,
                "phase": phase,
            }
        )

    def write_stage_complete(
        self,
        stage_id: str,
        stage_name: str,
        cost: float,
        duration: float,
    ) -> None:
        self.write_event(
            {
                "type": "stage_complete",
                "stage_id": stage_id,
                "stage_name": stage_name,
                "cost": cost,
                "duration": duration,
            }
        )

    def write_stage_failed(self, stage_id: str, stage_name: str, error: str) -> None:
        self.write_event(
            {
                "type": "stage_failed",
                "stage_id": stage_id,
                "stage_name": stage_name,
                "error": error,
            }
        )

    # ------------------------------------------------------------------
    # Workflow lifecycle
    # ------------------------------------------------------------------

    def write_workflow_complete(
        self,
        total_cost: float,
        total_duration: float,
        completed_count: int,
        total_stages: int,
    ) -> None:
        self.write_event(
            {
                "type": "workflow_complete",
                "total_cost": total_cost,
                "total_duration": total_duration,
                "completed_count": completed_count,
                "total_stages": total_stages,
            }
        )

    def write_workflow_error(self, error: str, stage_id: str | None = None) -> None:
        event: dict[str, Any] = {
            "type": "workflow_error",
            "error": error,
        }
        if stage_id is not None:
            event["stage_id"] = stage_id
        self.write_event(event)

    # ------------------------------------------------------------------
    # Gate + cost
    # ------------------------------------------------------------------

    def write_gate_result(
        self,
        stage_id: str,
        stage_name: str,
        *,
        approved: bool,
        reason: str = "",
    ) -> None:
        self.write_event(
            {
                "type": "gate_result",
                "stage_id": stage_id,
                "stage_name": stage_name,
                "approved": approved,
                "reason": reason,
            }
        )

    def write_cost_event(
        self,
        total_usd: float,
        stage_usd: float,
        budget_max: float,
        *,
        message: str = "",
    ) -> None:
        self.write_event(
            {
                "type": "cost_warning",
                "total_usd": total_usd,
                "stage_usd": stage_usd,
                "budget_max": budget_max,
                "message": message,
            }
        )
