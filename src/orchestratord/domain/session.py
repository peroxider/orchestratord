"""Session entity model (§5.2.3, §6.1.1).

A ``Session`` is one backend run (one ``BackendRunner.run``). It owns an
ordered stream of SPI ``EventEnvelope`` records — the execution log the Web
client replays as a timeline (§5.2.3). Model-level invariants:

* ``mode`` ∈ {single, pipeline, debate, swarm, coordinator} (§5.2.3
  multi-agent views). ``status`` is a free string, mirroring ``Issue.status``:
  the runtime vocabulary spans running / paused / pending_review / completed /
  stopped / failed / retrying, so pinning a subset here would drift.
* ``created_at`` is timezone-aware, matching the other entities.

Persistence (the ``sessions`` / ``events`` / ``approvals`` tables) is owned by
the repository layer (§6.1); this entity carries only validation, with no
in-memory side effects.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

_SESSION_MODES: frozenset[str] = frozenset(
    {"single", "pipeline", "debate", "swarm", "coordinator"}
)


def _normalize_created_at(value: datetime | None) -> datetime:
    """Return a tz-aware ``created_at``, matching the other entities."""
    if value is None:
        return datetime.now(UTC)
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


@dataclass
class Session:
    id: UUID
    workspace_id: UUID
    issue_id: UUID | None = None
    agent_id: UUID | None = None
    run_id: UUID | None = None
    mode: str = "single"
    status: str = "running"
    created_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.mode not in _SESSION_MODES:
            raise ValueError(
                f"invalid mode {self.mode!r}; expected one of "
                f"{sorted(_SESSION_MODES)}"
            )
        self.created_at = _normalize_created_at(self.created_at)


__all__ = ["Session"]
