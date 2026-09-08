"""Session entity invariants (§5.2.3, §6.1.1).

A session is one backend run. Model-level invariants:

* ``mode`` ∈ {single, pipeline, debate, swarm, coordinator}.
* ``created_at`` is timezone-aware, matching the other entities.

Persistence (the ``sessions`` / ``events`` / ``approvals`` tables) is owned by
the repository layer (§6.1); this entity carries only validation, with no
in-memory side effects.

Reference: docs/FEATURE_GAP_VS_MULTICA.md §5.2.3, §6.1.1.
"""
from __future__ import annotations

from uuid import uuid4

import pytest

from orchestratord.domain.session import Session


def _session(**overrides) -> Session:
    defaults = {"id": uuid4(), "workspace_id": uuid4()}
    defaults.update(overrides)
    return Session(**defaults)


class TestSessionFields:
    def test_required_fields_present(self) -> None:
        s = _session()
        for key in ("id", "workspace_id", "mode", "status", "created_at"):
            assert getattr(s, key) is not None, f"missing {key!r}"

    def test_defaults(self) -> None:
        s = _session()
        assert s.mode == "single"
        assert s.status == "running"
        assert s.issue_id is None
        assert s.agent_id is None
        assert s.run_id is None

    def test_created_at_is_timezone_aware(self) -> None:
        assert _session().created_at.tzinfo is not None


class TestModeWhitelist:
    def test_all_modes_accepted(self) -> None:
        for mode in ("single", "pipeline", "debate", "swarm", "coordinator"):
            assert _session(mode=mode).mode == mode

    def test_unknown_mode_rejected(self) -> None:
        with pytest.raises(ValueError, match="mode"):
            _session(mode="matrix")
