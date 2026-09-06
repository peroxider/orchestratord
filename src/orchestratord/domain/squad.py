"""Squad entity invariants (§7.1).

Squads group members (humans or agents) under a leader that routes work.
Model-level invariants:

* Leader polymorphism: ``leader_type`` ∈ {member, agent}; ``leader_id`` is a
  UUID matching that type.
* Member polymorphism: ``squad_members`` rows carry
  ``(member_type, member_id)``.
* Leader capability: when ``leader_type=agent``, the referenced agent's
  capabilities must include ``goal_mode=True`` (a non-goal-mode agent cannot
  route work). Enforced at the API layer (squads router), which resolves the
  leader through the agent repository.
* Self-leader prevention: a squad with a sole member cannot have that member
  be the leader (no candidates to route to).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID

_LEADER_TYPES = frozenset({"member", "agent"})
_MEMBER_TYPES = frozenset({"member", "agent"})


@dataclass
class SquadMember:
    member_type: str
    member_id: UUID

    def __post_init__(self) -> None:
        if self.member_type not in _MEMBER_TYPES:
            raise ValueError(f"invalid member_type {self.member_type!r}")


@dataclass
class Squad:
    id: UUID
    workspace_id: UUID
    name: str
    leader_type: str
    leader_id: UUID
    members: list[SquadMember] = field(default_factory=list)
    created_at: datetime | None = None
    is_deleted: bool = field(default=False, repr=False)

    def __post_init__(self) -> None:
        if self.leader_type not in _LEADER_TYPES:
            raise ValueError(f"invalid leader_type {self.leader_type!r}")

        # Self-leader prevention: a single-member squad cannot have that
        # member be the leader (no candidates to route to).
        if self.leader_type == "member" and len(self.members) == 1:
            sole = self.members[0]
            if sole.member_id == self.leader_id and sole.member_type == "member":
                raise ValueError(
                    "a squad with a sole member cannot have that member "
                    "as its leader"
                )

    def can_route(self) -> bool:
        """Return whether the squad has at least one non-leader candidate."""
        for member in self.members:
            if (member.member_id, member.member_type) != (
                self.leader_id,
                self.leader_type,
            ):
                return True
        return False

    def mark_deleted(self) -> None:
        """Soft-delete the squad; referenced members/agents are untouched."""
        self.is_deleted = True
