"""Metadata-reflection contract tests for the §6.1 schema (no live DB).

These tests reflect ``Base.metadata`` and assert the schema shape without a
database connection: the full 33-table inventory, the no-``ForeignKey`` rule
(§6.1), model↔domain field-name parity, and the ``events`` composite-PK /
JSONB / RANGE-partition special-casing (§6.1.2).
"""

from __future__ import annotations

from dataclasses import fields

import pytest
from sqlalchemy.dialects.postgresql import JSONB

import orchestratord.db  # noqa: F401  (registers all tables on Base.metadata)
from orchestratord.db.base import Base
from orchestratord.domain.agent import Agent
from orchestratord.domain.audit import AuditLogEntry
from orchestratord.domain.auth_token import AuthToken
from orchestratord.domain.autopilot import Autopilot
from orchestratord.domain.channel import Channel
from orchestratord.domain.inbox import InboxItem
from orchestratord.domain.integration import Integration
from orchestratord.domain.issue import (
    Issue,
    IssueComment,
    IssueLabel,
    IssueStatusChange,
)
from orchestratord.domain.project import Project
from orchestratord.domain.session import Session
from orchestratord.domain.workspace import Member, MemberAgentScope, Workspace

EXPECTED_TABLES = {
    "workspaces",
    "members",
    "member_agent_scopes",
    "agents",
    "agent_capabilities_cache",
    "runtimes",
    "runtime_backends",
    "issues",
    "issue_comments",
    "issue_labels",
    "issue_status_history",
    "sessions",
    "messages",
    "runs",
    "events",
    "approvals",
    "skills",
    "skill_source_maps",
    "skill_references",
    "inbox",
    "installations",
    "integrations",
    "usage_aggregates",
    "squads",
    "squad_members",
    "projects",
    "project_repos",
    "project_docs",
    "pull_requests",
    "autopilots",
    "autopilot_runs",
    "audit_log",
    "auth_tokens",
    "channels",
}

# Strict 1:1 parity between a table and its domain entity: every dataclass
# field name equals a column name and vice-versa. Entities carrying transient
# collections (``Squad.members``, ``Runtime.probed_backends``) or aggregate
# columns (``UsageAggregate.sessions`` vs ``UsageRecord.recorded_at``) are
# deliberately excluded from this exact diff.
PARITY = {
    "workspaces": Workspace,
    "members": Member,
    "member_agent_scopes": MemberAgentScope,
    "agents": Agent,
    "issues": Issue,
    "issue_comments": IssueComment,
    "issue_labels": IssueLabel,
    "issue_status_history": IssueStatusChange,
    "sessions": Session,
    "audit_log": AuditLogEntry,
    "auth_tokens": AuthToken,
    "channels": Channel,
    "integrations": Integration,
    "inbox": InboxItem,
    "projects": Project,
    "autopilots": Autopilot,
}

# Every JSONB column across the schema (§6.1.1 / §6.1.2 payload GIN).
JSONB_COLUMNS = {
    "agents": {"capabilities_cache_jsonb"},
    "agent_capabilities_cache": {"capabilities_jsonb", "model_pricing_jsonb"},
    "skills": {"allowed_tools", "stale_reasons"},
    "events": {"payload"},
    "audit_log": {"payload_jsonb"},
    "auth_tokens": {"scopes"},
}


def test_all_tables_present() -> None:
    assert set(Base.metadata.tables) == EXPECTED_TABLES


def test_no_foreign_keys() -> None:
    for table in Base.metadata.tables.values():
        for column in table.columns:
            assert not column.foreign_keys, (
                f"{table.name}.{column.name} declares a ForeignKey "
                "(§6.1 forbids FK / cascading deletes)"
            )


@pytest.mark.parametrize(("table_name", "entity"), sorted(PARITY.items()))
def test_column_parity(table_name: str, entity: type) -> None:
    table = Base.metadata.tables[table_name]
    expected = {f.name for f in fields(entity)}
    actual = set(table.columns.keys())
    assert actual == expected


def test_events_composite_primary_key() -> None:
    events = Base.metadata.tables["events"]
    assert {c.name for c in events.primary_key.columns} == {"id", "created_at"}


def test_events_payload_is_jsonb() -> None:
    events = Base.metadata.tables["events"]
    assert isinstance(events.c.payload.type, JSONB)


def test_events_partition_by_range() -> None:
    events = Base.metadata.tables["events"]
    assert events.dialect_options["postgresql"]["partition_by"] == "RANGE (created_at)"


@pytest.mark.parametrize("table_name", sorted(JSONB_COLUMNS))
def test_jsonb_columns(table_name: str) -> None:
    table = Base.metadata.tables[table_name]
    for column_name in JSONB_COLUMNS[table_name]:
        assert isinstance(table.c[column_name].type, JSONB), (
            f"{table_name}.{column_name} is not JSONB"
        )
