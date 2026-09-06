"""Workspace / member / role entities (§5.7.1, §6.1).

Multi-tenancy foundation: a workspace is the tenant boundary, keyed by
``slug`` for routing; members belong to a workspace and carry one of three
roles; ``member_agent_scopes`` is the many-to-many join granting a member
access to specific agents.

Invariants:

* ``role`` ∈ {owner, admin, member} (§5.7.1 role matrix).
* ``created_at`` is timezone-aware, matching the other entities.

Reference: docs/FEATURE_GAP_VS_MULTICA.md §5.7.1, §6.1.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

_ROLES = frozenset({"owner", "admin", "member"})


def _normalize_created_at(value: datetime | None) -> datetime:
    """Return a tz-aware ``created_at``, matching the other entities."""
    if value is None:
        return datetime.now(UTC)
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


@dataclass
class Workspace:
    id: UUID
    slug: str
    name: str
    created_at: datetime | None = None

    def __post_init__(self) -> None:
        self.created_at = _normalize_created_at(self.created_at)


@dataclass
class Member:
    id: UUID
    workspace_id: UUID
    role: str
    name: str = ""
    created_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.role not in _ROLES:
            raise ValueError(
                f"invalid role {self.role!r}; expected one of {sorted(_ROLES)}"
            )
        self.created_at = _normalize_created_at(self.created_at)


@dataclass
class MemberAgentScope:
    member_id: UUID
    agent_id: UUID


__all__ = ["Member", "MemberAgentScope", "Workspace"]
