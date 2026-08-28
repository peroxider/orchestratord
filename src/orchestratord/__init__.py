"""Orchestrator subsystem for autonomous mode."""

from ._version import __version__, __version_info__
from .backend_runner import BackendRunner
from .session_state import AgentSession

# The public runner is backend-neutral. Keep the historical name as an alias
# so downstream code can migrate without making a concrete backend implicit.
AgentRunner = BackendRunner
from .agent_task import AgentTask, AgentTaskResult, ProgressEvent, ProgressEventKind
from .agent_task_runner import AgentTaskRunner
from .config.schema import WorkflowConfig
from .issue import Issue
from .issue_to_task import issue_to_agent_task
from .linear.adapter import LinearAdapter
from .linear.client import LinearGraphQLClient
from .local_tracker.adapter import LocalTrackerAdapter
from .orchestrator import Orchestrator
from .prompt_builder import PromptBuilder
from .repo_tracker.adapter import RepositoryTrackerAdapter
from .status_dashboard import StatusDashboard
from .tracker import TrackerAdapter, create_tracker_adapter
from .workflow import WorkflowLoader, WorkflowParseError
from .workspace import Workspace, WorkspaceConfig, WorkspaceManager

__all__ = [
    "AgentRunner",
    "AgentSession",
    "AgentTask",
    "AgentTaskResult",
    "AgentTaskRunner",
    "LinearAdapter",
    "LinearGraphQLClient",
    "LocalTrackerAdapter",
    "Issue",
    "Orchestrator",
    "ProgressEvent",
    "ProgressEventKind",
    "PromptBuilder",
    "RepositoryTrackerAdapter",
    "StatusDashboard",
    "TrackerAdapter",
    "create_tracker_adapter",
    "issue_to_agent_task",
    "WorkflowConfig",
    "WorkflowLoader",
    "WorkflowParseError",
    "Workspace",
    "WorkspaceConfig",
    "WorkspaceManager",
]
