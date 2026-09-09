"""Orchestrator subsystem for autonomous mode."""

from ._version import __version__, __version_info__
from .agent.task import AgentTask, AgentTaskResult, ProgressEvent, ProgressEventKind
from .agent.runner import AgentTaskRunner
# P6（DESIGN §6 :420/:435）：prompts 注册触发——旧路径经 orchestratord.orchestrator
# 的模块级联（orchestrator.py 顶层 import applications.issue_pr.prompts）实现
# 「orchestratord 包首次 import 即级联注册」；orchestrator 兼容 re-export 删除后
# 由本直连 import 保持同一注册时序（applications/issue_pr/__init__ 同样 import prompts）。
from .applications.issue_pr import prompts as _issue_pr_prompt_registration  # noqa: F401
from .backend_runner import BackendRunner
from .conversation_store import ConversationStore, ensure_conversation_id
from .session_state import AgentSession
from .config.schema import WorkflowConfig
from .issue_registry.issue import Issue
from .issue_registry.task_mapping import issue_to_agent_task
from .linear.adapter import LinearAdapter
from .linear.client import LinearGraphQLClient
from .local_tracker.adapter import LocalTrackerAdapter
from .prompt_builder import PromptBuilder
from .repo_tracker.adapter import RepositoryTrackerAdapter
from .status_dashboard import StatusDashboard
from .tracker import TrackerAdapter, create_tracker_adapter
from .workflow import WorkflowLoader, WorkflowParseError
from .workspace import Workspace, WorkspaceConfig, WorkspaceManager

__all__ = [
    "AgentSession",
    "AgentTask",
    "AgentTaskResult",
    "AgentTaskRunner",
    "BackendRunner",
    "ConversationStore",
    "ensure_conversation_id",
    "LinearAdapter",
    "LinearGraphQLClient",
    "LocalTrackerAdapter",
    "Issue",
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
