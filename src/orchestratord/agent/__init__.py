"""Agent task abstraction — generic work-unit for the orchestration layer."""

from .task import AgentTask, AgentTaskResult, ProgressEvent, ProgressEventKind
from .runner import AgentTaskRunner

__all__ = [
    "AgentTask",
    "AgentTaskResult",
    "AgentTaskRunner",
    "ProgressEvent",
    "ProgressEventKind",
]
