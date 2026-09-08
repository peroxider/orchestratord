"""Autopilot entity model (§7.3).

An autopilot is a cron-like periodic task that launches a declarative
workflow. ``Autopilot`` is the schedule definition; ``AutopilotRun`` is one
execution record (schedule → start → finish) linking back to the executed
workflow ``run_id``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID


@dataclass
class Autopilot:
    id: UUID
    workspace_id: UUID
    name: str
    cron: str
    prompt: str
    target_kind: str
    target_id: UUID
    enabled: bool = True


@dataclass
class AutopilotRun:
    autopilot_id: UUID
    scheduled_at: datetime
    run_id: UUID
    status: str
    started_at: datetime | None = None
    finished_at: datetime | None = None
