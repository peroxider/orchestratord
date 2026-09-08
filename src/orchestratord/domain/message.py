"""Chat message entity (``docs/FEATURE_GAP_VS_MULTICA.md`` §6.1).

A :class:`Message` is one conversational turn in a session's chat timeline. It
is the higher-level shape the chat UI renders — each row collapses a span of
SPI events (one ``user`` POST, or many ``text_delta`` events folded into a
single ``assistant`` turn) into a renderable unit. Persistence is in the
``messages`` table (migration 0043); this entity carries only validation.

Single-user mode (§4): there is no ``author_id`` referencing a member. The
caller identifies the user with ``role="user"`` and an opaque ``author_label``
(typically ``"me"`` or a configured alias). Assistant turns record the
producing ``agent_id`` so the UI can render the agent avatar.

Model-level invariants:

* ``role`` ∈ {user, assistant, system, tool}. Anything outside this set is a
  schema error — the chat renderer only knows those four slots. ``tool`` is
  reserved for tool-result summaries that the chat composer chooses to
  surface inline; raw tool events still flow through ``events`` (§5.2.3).
* ``seq`` is a per-session monotonically increasing integer assigned by the
  repository at insert time. The chat timeline sorts by ``(session_id, seq)``
  so concurrent appends from the runner don't interleave with user posts.
* ``created_at`` is timezone-aware, matching the rest of the §6.1 entities.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

_MESSAGE_ROLES: frozenset[str] = frozenset({"user", "assistant", "system", "tool"})


def _normalize_created_at(value: datetime | None) -> datetime:
    """Return a tz-aware ``created_at``, matching the other entities."""
    if value is None:
        return datetime.now(UTC)
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


@dataclass
class Message:
    id: UUID
    session_id: UUID
    workspace_id: UUID
    seq: int
    role: str
    content: str
    agent_id: UUID | None = None
    author_label: str | None = None
    created_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.role not in _MESSAGE_ROLES:
            raise ValueError(
                f"invalid role {self.role!r}; expected one of "
                f"{sorted(_MESSAGE_ROLES)}"
            )
        if self.seq < 0:
            raise ValueError(f"seq must be non-negative; got {self.seq}")
        if not self.content and self.role != "tool":
            raise ValueError("content must be non-empty for user/assistant/system roles")
        self.created_at = _normalize_created_at(self.created_at)


__all__ = ["Message"]
