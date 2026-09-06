"""Inbox entity model (``docs/FEATURE_GAP_VS_MULTICA.md`` §5.2.6, §6.1.1).

An inbox item is a human-intervention event ("pinged"): APPROVAL_REQUEST,
clarification, or failure. Each item links back to its originating
issue / session / event so the Web client can jump to context. The in-memory
registry stands in for the ``inbox`` table until the repository layer lands.

Model invariants:

* ``kind`` ∈ {approval_request, clarification, failed}
* ``status`` is a small lifecycle: open → assigned → resolved / dismissed.
  ``assign`` is only valid from ``open``; ``resolve`` / ``dismiss`` are
  terminal (cannot be re-applied).
* ``assignee_type`` ∈ {member, agent}; set together with ``assignee_id``.
* ``created_at`` is timezone-aware.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

_KINDS = frozenset({"approval_request", "clarification", "failed"})
_ASSIGNEE_TYPES = frozenset({"member", "agent"})
_STATUSES = frozenset({"open", "assigned", "resolved", "dismissed"})
_TERMINAL_STATUSES = frozenset({"resolved", "dismissed"})


def _normalize_created_at(value: datetime | None) -> datetime:
    """Return a tz-aware ``created_at``, matching the other entities."""
    if value is None:
        return datetime.now(UTC)
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


@dataclass
class InboxItem:
    id: UUID
    workspace_id: UUID
    kind: str
    title: str
    issue_id: UUID | None = None
    session_id: UUID | None = None
    event_seq: int | None = None
    status: str = "open"
    assignee_type: str | None = None
    assignee_id: UUID | None = None
    created_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.kind not in _KINDS:
            raise ValueError(
                f"invalid kind {self.kind!r}; expected one of {sorted(_KINDS)}"
            )
        if self.status not in _STATUSES:
            raise ValueError(f"invalid status {self.status!r}")
        if (self.assignee_type is None) != (self.assignee_id is None):
            raise ValueError("assignee_type and assignee_id must be both set or both None")
        if self.assignee_type is not None and self.assignee_type not in _ASSIGNEE_TYPES:
            raise ValueError(
                f"invalid assignee_type {self.assignee_type!r}; "
                f"expected one of {sorted(_ASSIGNEE_TYPES)}"
            )
        self.created_at = _normalize_created_at(self.created_at)

    def assign(self, assignee_type: str, assignee_id: UUID) -> None:
        """Assign the item to a member or agent (only valid from ``open``)."""
        if assignee_type not in _ASSIGNEE_TYPES:
            raise ValueError(
                f"invalid assignee_type {assignee_type!r}; "
                f"expected one of {sorted(_ASSIGNEE_TYPES)}"
            )
        if self.status != "open":
            raise ValueError(f"cannot assign an inbox item in status {self.status!r}")
        self.assignee_type = assignee_type
        self.assignee_id = assignee_id
        self.status = "assigned"

    def resolve(self) -> None:
        """Mark the item resolved (terminal)."""
        if self.status in _TERMINAL_STATUSES:
            raise ValueError(f"cannot resolve an inbox item in status {self.status!r}")
        self.status = "resolved"

    def dismiss(self) -> None:
        """Dismiss the item (terminal)."""
        if self.status in _TERMINAL_STATUSES:
            raise ValueError(f"cannot dismiss an inbox item in status {self.status!r}")
        self.status = "dismissed"


__all__ = ["InboxItem"]
