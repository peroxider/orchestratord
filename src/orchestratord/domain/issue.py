"""Issue entity models (§5.2.1, §6.1.1).

DB-backed mirror of the ``issues`` table and its activity-child tables
(``issue_comments``, ``issue_labels``, ``issue_status_history``). The load-
bearing invariant is the assignee polymorphism: an issue is assigned to
either a member or an agent via ``(assignee_type, assignee_id)``, and the two
fields must be set or cleared together.

``status`` is deliberately a plain string rather than a whitelist: the
existing orchestrator ``IssueStatus`` enum carries 10 stages while §5.2.1
lists a smaller display set, so pinning a fixed set here would drift from the
runtime. The model enforces shape, not the status vocabulary.

Reference: docs/FEATURE_GAP_VS_MULTICA.md §5.2.1, §6.1.1.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

_ASSIGNEE_TYPES = frozenset({"member", "agent"})


def _normalize_created_at(value: datetime | None) -> datetime:
    """Return a tz-aware ``created_at``, matching the other entities."""
    if value is None:
        return datetime.now(UTC)
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


@dataclass
class Issue:
    id: UUID
    workspace_id: UUID
    title: str
    description: str = ""
    status: str = "pending"
    assignee_type: str | None = None
    assignee_id: UUID | None = None
    created_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.assignee_type is not None and self.assignee_type not in _ASSIGNEE_TYPES:
            raise ValueError(
                f"invalid assignee_type {self.assignee_type!r}; "
                f"expected one of {sorted(_ASSIGNEE_TYPES)}"
            )
        if (self.assignee_type is None) != (self.assignee_id is None):
            raise ValueError("assignee_type and assignee_id must be both set or both None")
        self.created_at = _normalize_created_at(self.created_at)


@dataclass
class IssueComment:
    id: UUID
    issue_id: UUID
    author_type: str
    author_id: UUID
    body: str
    created_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.author_type not in _ASSIGNEE_TYPES:
            raise ValueError(
                f"invalid author_type {self.author_type!r}; "
                f"expected one of {sorted(_ASSIGNEE_TYPES)}"
            )
        self.created_at = _normalize_created_at(self.created_at)


@dataclass
class IssueLabel:
    issue_id: UUID
    name: str


@dataclass
class IssueStatusChange:
    id: UUID
    issue_id: UUID
    from_status: str | None
    to_status: str
    created_at: datetime | None = None

    def __post_init__(self) -> None:
        self.created_at = _normalize_created_at(self.created_at)


__all__ = ["Issue", "IssueComment", "IssueLabel", "IssueStatusChange"]
