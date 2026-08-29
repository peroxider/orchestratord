"""Orchestrator → IM event bridge (P3).

Defines the event types and the event → IM text formatter. Per-sink
exception isolation is baked in so an IM failure never breaks the
orchestrator main flow.
"""

from __future__ import annotations

from .emitter import OrchestratorEventEmitter
from .formatter import format_event
from .types import EventLevel, OrchestratorEvent

__all__ = [
    "EventLevel",
    "OrchestratorEvent",
    "OrchestratorEventEmitter",
    "format_event",
]
