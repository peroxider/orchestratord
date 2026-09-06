"""Aggregate re-export of every §6.1.1 ORM model.

Importing this package registers all tables on ``Base.metadata`` so Alembic
autogenerate and the metadata reflection contract tests see the full schema.
"""

from orchestratord.db.models.agents import (
    Agent,
    AgentCapabilitiesCache,
    Runtime,
    RuntimeBackend,
)
from orchestratord.db.models.audit_auth import AuditLogEntry, AuthToken, Channel
from orchestratord.db.models.collab import (
    Autopilot,
    AutopilotRun,
    Project,
    ProjectDoc,
    ProjectRepo,
    Squad,
    SquadMember,
)
from orchestratord.db.models.inbox import InboxItem, UsageAggregate
from orchestratord.db.models.integrations import Integration
from orchestratord.db.models.issues import (
    Issue,
    IssueComment,
    IssueLabel,
    IssueStatusChange,
)
from orchestratord.db.models.sessions import Approval, Event, Run, Session
from orchestratord.db.models.skills import Skill, SkillReference, SkillSourceMap
from orchestratord.db.models.tenancy import Member, MemberAgentScope, Workspace
from orchestratord.db.models.vcs import GitHubInstallation, PullRequest

__all__ = [
    "Agent",
    "AgentCapabilitiesCache",
    "Approval",
    "AuditLogEntry",
    "AuthToken",
    "Autopilot",
    "AutopilotRun",
    "Channel",
    "Event",
    "GitHubInstallation",
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
    "PullRequest",
    "Run",
    "Runtime",
    "RuntimeBackend",
    "Session",
    "Skill",
    "SkillReference",
    "SkillSourceMap",
    "Squad",
    "SquadMember",
    "UsageAggregate",
    "Workspace",
]
