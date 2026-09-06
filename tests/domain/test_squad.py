"""Squad entity invariants (§7.1).

Squads group members (humans or agents) under a leader that routes work.
Invariants:

* Leader polymorphism: ``leader_type ∈ {member, agent}``; ``leader_id``
  is a UUID matching that type.
* Member polymorphism: ``squad_members`` rows carry
  ``(member_type, member_id)``.
* Leader capability (the agent goal-mode gate) is enforced at the API
  layer, not in this entity.
* Self-leader prevention: a squad with a single member cannot have that
  member be the leader (no candidates to route to).
* Cascade semantics: deleting a squad removes its ``squad_members``
  rows; referenced members/agents remain.

Reference: docs/FEATURE_GAP_VS_MULTICA.md §7.1; multica ``Squads``.
"""
from __future__ import annotations

from uuid import uuid4

import pytest


def _squad(**overrides):
    from orchestratord.domain.squad import Squad  # type: ignore

    defaults = {
        "id": uuid4(),
        "workspace_id": uuid4(),
        "name": "core-team",
        "leader_type": "member",
        "leader_id": uuid4(),
        "members": [],
        "created_at": None,
    }
    defaults.update(overrides)
    return Squad(**defaults)


class TestLeaderPolymorphism:
    """``leader_type`` is constrained to ``member`` / ``agent``."""

    def test_member_leader_accepted(self) -> None:
        sq = _squad(leader_type="member")
        assert sq.leader_type == "member"

    def test_agent_leader_accepted(self) -> None:
        sq = _squad(leader_type="agent")
        assert sq.leader_type == "agent"

    def test_invalid_leader_type_rejected(self) -> None:
        with pytest.raises(ValueError):
            _squad(leader_type="bot")


class TestMemberPolymorphism:
    """SquadMember rows carry ``member_type`` + ``member_id``."""

    def test_mixed_member_types_allowed(self) -> None:
        from orchestratord.domain.squad import SquadMember  # type: ignore

        sq = _squad(
            leader_type="member",
            members=[
                SquadMember(member_type="member", member_id=uuid4()),
                SquadMember(member_type="agent", member_id=uuid4()),
            ],
        )
        types = {m.member_type for m in sq.members}
        assert types == {"member", "agent"}

    def test_invalid_member_type_rejected(self) -> None:
        from orchestratord.domain.squad import SquadMember  # type: ignore

        with pytest.raises(ValueError):
            SquadMember(member_type="service_account", member_id=uuid4())


class TestSelfLeaderPrevention:
    """A squad with one member cannot have that member be the leader."""

    def test_solo_squad_with_self_leader_rejected(self) -> None:
        from orchestratord.domain.squad import Squad, SquadMember  # type: ignore

        leader_id = uuid4()
        with pytest.raises(ValueError, match="sole member"):
            Squad(
                id=uuid4(),
                workspace_id=uuid4(),
                name="solo",
                leader_type="member",
                leader_id=leader_id,
                members=[SquadMember(member_type="member", member_id=leader_id)],
            )


class TestRoutingEligibility:
    """A squad can only route work when it has ≥1 non-leader member."""

    def test_empty_squad_cannot_route(self) -> None:
        sq = _squad(leader_type="member", members=[])
        assert sq.can_route() is False

    def test_squad_with_candidate_can_route(self) -> None:
        from orchestratord.domain.squad import SquadMember  # type: ignore

        sq = _squad(
            leader_type="member", leader_id=uuid4(),
            members=[SquadMember(member_type="member", member_id=uuid4())],
        )
        assert sq.can_route() is True


class TestCascadeSemantics:
    """Deleting a squad must not cascade to referenced members."""

    def test_delete_isolates_members(self) -> None:
        from orchestratord.domain.squad import SquadMember  # type: ignore

        member_id = uuid4()
        sq = _squad(
            leader_type="member", leader_id=uuid4(),
            members=[SquadMember(member_type="member", member_id=member_id)],
        )
        sq.mark_deleted()
        assert sq.is_deleted is True
