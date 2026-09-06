"""Domain entity models (§5.7.3, §6.2, §6.3, §7.1–§7.3).

In-memory, DB-agnostic entities that mirror the PostgreSQL schema (§6.1.1)
without depending on a driver. Persistence and repository layers land in
Phase 2; until then these classes enforce the model-level invariants the
contract tests pin. They are deliberately free of FastAPI / SQLAlchemy
imports so the Web layer and the daemon can share them.
"""

from orchestratord.domain.agent import Agent
from orchestratord.domain.audit import AuditLogEntry
from orchestratord.domain.auth_token import (
    AuthToken,
    hash_api_token,
    issue_api_token,
    verify_api_token,
)
from orchestratord.domain.autopilot import Autopilot, AutopilotRun
from orchestratord.domain.channel import Channel
from orchestratord.domain.inbox import InboxItem
from orchestratord.domain.integration import Integration
from orchestratord.domain.issue import (
    Issue,
    IssueComment,
    IssueLabel,
    IssueStatusChange,
)
from orchestratord.domain.mention import parse_mentions
from orchestratord.domain.project import Project, ProjectDoc, ProjectRepo
from orchestratord.domain.runtime import (
    Runtime,
    RuntimeStatus,
    hash_runtime_token,
    issue_runtime_token,
    verify_runtime_token,
)
from orchestratord.domain.session import Session
from orchestratord.domain.squad import Squad, SquadMember
from orchestratord.domain.usage import (
    UsageRecord,
    aggregate_usage,
    aggregate_usage_rows,
    usage_totals,
    usage_totals_rows,
)
from orchestratord.domain.workspace import Member, MemberAgentScope, Workspace

__all__ = [
    "Agent",
    "AuditLogEntry",
    "AuthToken",
    "Autopilot",
    "AutopilotRun",
    "Channel",
    "InboxItem",
    "Integration",
    "Issue",
    "IssueComment",
    "IssueLabel",
    "IssueStatusChange",
    "Member",
    "MemberAgentScope",
    "Project",
    "ProjectDoc",
    "ProjectRepo",
    "Runtime",
    "RuntimeStatus",
    "Session",
    "Squad",
    "SquadMember",
    "UsageRecord",
    "Workspace",
    "aggregate_usage",
    "aggregate_usage_rows",
    "hash_api_token",
    "hash_runtime_token",
    "issue_api_token",
    "issue_runtime_token",
    "parse_mentions",
    "usage_totals",
    "usage_totals_rows",
    "verify_api_token",
    "verify_runtime_token",
]
