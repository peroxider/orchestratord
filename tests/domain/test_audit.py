"""Audit log entity invariants (§5.7.3).

Every Web-triggered mutation writes an ``audit_log`` row. Invariants:

* ``actor_type`` ∈ {member, agent, system}.
* ``created_at`` is timezone-aware.

Reference: docs/FEATURE_GAP_VS_MULTICA.md §5.7.3.
"""
from __future__ import annotations

from uuid import uuid4

import pytest


def _entry(**overrides):
    from orchestratord.domain.audit import AuditLogEntry  # type: ignore

    defaults = {
        "id": uuid4(),
        "workspace_id": uuid4(),
        "actor_type": "member",
        "actor_id": uuid4(),
        "action": "issue.create",
        "target_type": "issue",
        "target_id": uuid4(),
        "payload_jsonb": {},
    }
    defaults.update(overrides)
    return AuditLogEntry(**defaults)


class TestActorType:
    """``actor_type`` is constrained to {member, agent, system}."""

    def test_valid_actor_types_accepted(self) -> None:
        for kind in ("member", "agent", "system"):
            e = _entry(actor_type=kind)
            assert e.actor_type == kind

    def test_invalid_actor_type_rejected(self) -> None:
        with pytest.raises(ValueError, match="actor_type"):
            _entry(actor_type="service_account")


class TestCreatedAt:
    """``created_at`` is timezone-aware, matching the other entities."""

    def test_created_at_is_timezone_aware(self) -> None:
        e = _entry()
        assert e.created_at.tzinfo is not None


class TestPayload:
    """``payload_jsonb`` round-trips through JSON without losing detail."""

    def test_payload_round_trips(self) -> None:
        import json

        original = {"from": "queued", "to": "running", "by": "agent-7"}
        e = _entry(payload_jsonb=original)
        assert json.loads(json.dumps(e.payload_jsonb)) == original
