"""Workspace / member / role invariants (§5.7.1, §6.1).

Multi-tenancy foundation. Invariants:

* ``role`` ∈ {owner, admin, member} (§5.7.1 role matrix).
* ``member_agent_scopes`` is a (member_id, agent_id) many-to-many join.
* ``created_at`` is timezone-aware, matching the other entities.

Reference: docs/FEATURE_GAP_VS_MULTICA.md §5.7.1, §6.1.
"""
from __future__ import annotations

from uuid import uuid4

import pytest

from orchestratord.domain.workspace import Member, MemberAgentScope, Workspace


def _workspace(**overrides):
    defaults = {
        "id": uuid4(),
        "slug": "acme",
        "name": "Acme Corp",
    }
    defaults.update(overrides)
    return Workspace(**defaults)


def _member(**overrides):
    defaults = {
        "id": uuid4(),
        "workspace_id": uuid4(),
        "role": "member",
    }
    defaults.update(overrides)
    return Member(**defaults)


class TestWorkspaceFields:
    """A workspace is keyed by slug and carries a display name."""

    def test_required_fields_present(self) -> None:
        w = _workspace()
        for key in ("id", "slug", "name"):
            assert getattr(w, key) is not None, f"missing {key!r}"

    def test_slug_round_trips(self) -> None:
        assert _workspace(slug="acme").slug == "acme"

    def test_created_at_is_timezone_aware(self) -> None:
        assert _workspace().created_at.tzinfo is not None


class TestMemberRole:
    """``role`` is constrained to {owner, admin, member}."""

    def test_all_roles_accepted(self) -> None:
        for role in ("owner", "admin", "member"):
            m = _member(role=role)
            assert m.role == role

    def test_invalid_role_rejected(self) -> None:
        with pytest.raises(ValueError, match="role"):
            _member(role="viewer")

    def test_role_is_required(self) -> None:
        # A member must declare its role explicitly; there is no silent default.
        with pytest.raises(TypeError):
            Member(id=uuid4(), workspace_id=uuid4())


class TestMemberFields:
    """A member links to its workspace and carries a display name."""

    def test_workspace_link(self) -> None:
        ws = uuid4()
        assert _member(workspace_id=ws).workspace_id == ws

    def test_name_defaults_to_empty(self) -> None:
        assert _member().name == ""

    def test_created_at_is_timezone_aware(self) -> None:
        assert _member().created_at.tzinfo is not None


class TestMemberAgentScope:
    """The join table grants a member access to a specific agent."""

    def test_scope_round_trips(self) -> None:
        mid = uuid4()
        aid = uuid4()
        s = MemberAgentScope(member_id=mid, agent_id=aid)
        assert s.member_id == mid
        assert s.agent_id == aid
