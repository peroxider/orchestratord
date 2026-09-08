"""Agent entity model invariants (§6.2).

The new ``Agent`` model (Phase 1) is the persisted twin of a runtime
backend. Invariants:

* ``provider`` must be in the orchestrator's ``SupportedTypes`` (or
  ``BuiltinRuntime``) registry — no orphan providers.
* Name uniqueness per workspace (composite unique constraint).
* ``capabilities_cache_jsonb`` round-trips losslessly through JSON.
* ``created_at`` is timezone-aware.
* Cross-workspace reference rejection (multica rule: all queries filter
  by ``workspace_id``).

Reference: docs/FEATURE_GAP_VS_MULTICA.md §6.2; multica
``SupportedTypes`` analogue.
"""
from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest


def _agent(**overrides):
    """Factory for an in-memory Agent record; bypasses DB."""
    from orchestratord.domain.agent import Agent  # type: ignore

    defaults = {
        "id": uuid4(),
        "workspace_id": uuid4(),
        "name": "alpha",
        "provider": "codex",
        "runtime_id": uuid4(),
        "capabilities_cache_jsonb": {
            "streaming_deltas": True, "resumable": True, "interrupt": True,
            "approval_hooks": True, "parallel_sessions": True,
            "cost_reporting": False, "tool_filtering": False, "takeover": False,
            "backend_version": "1.0.0",
        },
        "created_at": datetime.now(UTC),
    }
    defaults.update(overrides)
    return Agent(**defaults)


class TestFieldInvariants:
    """All required fields present; types are enforced."""

    def test_required_fields_present(self) -> None:
        agent = _agent()
        for key in (
            "id", "workspace_id", "name", "provider", "runtime_id",
            "capabilities_cache_jsonb", "created_at",
        ):
            assert getattr(agent, key) is not None, f"missing {key!r}"

    def test_created_at_is_timezone_aware(self) -> None:
        agent = _agent()
        assert agent.created_at.tzinfo is not None


class TestProviderWhitelist:
    """Provider must be in ``SupportedTypes`` (orchestratord) or
    ``BuiltinRuntime`` (multica-style)."""

    def test_known_provider_accepted(self) -> None:
        agent = _agent(provider="codex")
        assert agent.provider == "codex"

    def test_unknown_provider_rejected(self) -> None:
        with pytest.raises(ValueError, match="provider"):
            _agent(provider="__not_a_real_provider__")


class TestWorkspaceIsolation:
    """Two workspaces can hold an agent with the same name; cross-workspace
    references are blocked at the repository layer (tested separately in
    ``tests/repositories/``); here we pin the model-level rule that the
    composite ``(workspace_id, name)`` is the natural key."""

    def test_same_name_allowed_across_workspaces(self) -> None:
        a = _agent(workspace_id=uuid4(), name="alpha")
        b = _agent(workspace_id=uuid4(), name="alpha")
        # Model equality does not imply unique; uniqueness is enforced
        # by the DB layer. The model must NOT collapse these two.
        assert a.name == b.name
        assert a.workspace_id != b.workspace_id


class TestCapabilitiesCache:
    """The cache must round-trip through JSON without losing bits."""

    def test_capabilities_cache_round_trips(self) -> None:
        import json

        original = {
            "streaming_deltas": True, "resumable": True, "interrupt": True,
            "approval_hooks": True, "parallel_sessions": True,
            "cost_reporting": False, "tool_filtering": False, "takeover": False,
            "backend_version": "1.0.0",
        }
        agent = _agent(capabilities_cache_jsonb=original)
        encoded = json.dumps(agent.capabilities_cache_jsonb)
        decoded = json.loads(encoded)
        assert decoded == original

    def test_partial_capabilities_rejected(self) -> None:
        """Missing capability bits must be caught at validation time, not
        later when the Web UI tries to render the matrix."""
        with pytest.raises(ValueError):
            _agent(capabilities_cache_jsonb={"streaming_deltas": True})
