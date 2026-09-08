"""Audit log entity model (§5.7.3).

Every Web-triggered mutation (create issue / reassign / approve) writes an
``audit_log`` row. ``actor_type`` distinguishes who acted — member / agent /
system. ``created_at`` is timezone-aware; ``payload_jsonb`` holds arbitrary
mutation detail.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

_ACTOR_TYPES = frozenset({"member", "agent", "system"})


@dataclass
class AuditLogEntry:
    id: UUID
    workspace_id: UUID
    actor_type: str
    actor_id: UUID | str
    action: str
    target_type: str
    target_id: UUID | str
    payload_jsonb: dict[str, Any] | None = None
    created_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.actor_type not in _ACTOR_TYPES:
            raise ValueError(
                f"invalid actor_type {self.actor_type!r}; expected one of "
                f"{sorted(_ACTOR_TYPES)}"
            )
        if self.created_at is None:
            self.created_at = datetime.now(UTC)
        elif self.created_at.tzinfo is None:
            self.created_at = self.created_at.replace(tzinfo=UTC)
