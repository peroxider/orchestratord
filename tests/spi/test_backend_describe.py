"""BackendRunner.describe() contract (§6.2).

Phase 1 introduces a new method on ``BackendRunner`` that exposes the
backend's ``BackendCapabilities`` as a pure-data descriptor for caching
in the ``agent_capabilities_cache`` DB column. The contract:

* MUST NOT mutate ``BackendCapabilities`` (no degradation in describe).
* MUST surface every one of the 8 capability bits defined in
  ``src/orchestratord/spi/capabilities.py``.
* MUST include pricing/model info when ``cost_reporting=True``.
* MUST be cacheable (deterministic + JSON-serializable).
* MUST NOT invoke any external agent CLI during describe — capability
  probing happens at backend construction, not here.

Reference: docs/FEATURE_GAP_VS_MULTICA.md §6.2.
"""
from __future__ import annotations

import pytest


def _fake_backend(capabilities_kwargs: dict) -> object:
    """Stand-in for a real ``AgentBackend`` returned by ``resolve_backend``."""
    from orchestratord.spi.capabilities import BackendCapabilities

    class _FakeBackend:
        capabilities = BackendCapabilities(**capabilities_kwargs)
        family = "Cli"
        version = "1.0.0"

    return _FakeBackend()


@pytest.fixture
def stub_runner(monkeypatch):
    """Build a ``BackendRunner`` whose ``backend_factory`` returns a fake."""
    from orchestratord.backend_runner import BackendRunner

    fake = _fake_backend({
        "streaming_deltas": True,
        "resumable": False,
        "interrupt": False,
        "approval_hooks": False,
        "parallel_sessions": True,
        "cost_reporting": False,
        "tool_filtering": False,
        "takeover": False,
    })
    monkeypatch.setattr(
        "orchestratord.backend_runner.resolve_backend",
        lambda name, cfg: fake,
    )
    return BackendRunner(backend_name="fake", config={})


class TestDescribeReturnsCapabilitiesAsIs:
    """``describe()`` MUST NOT degrade the backend's report."""

    def test_capability_bits_round_trip_unchanged(self, stub_runner) -> None:
        desc = stub_runner.describe()
        for bit in (
            "streaming_deltas", "resumable", "interrupt", "approval_hooks",
            "parallel_sessions", "cost_reporting", "tool_filtering",
            "takeover",
        ):
            assert hasattr(desc, bit), f"describe() missing capability bit {bit!r}"

    def test_describe_does_not_degrade(self, monkeypatch) -> None:
        """A backend reporting streaming=True must surface streaming=True."""
        from orchestratord.backend_runner import BackendRunner

        fake = _fake_backend({
            "streaming_deltas": True, "resumable": True, "interrupt": True,
            "approval_hooks": True, "parallel_sessions": True,
            "cost_reporting": True, "tool_filtering": True, "takeover": True,
        })
        monkeypatch.setattr(
            "orchestratord.backend_runner.resolve_backend",
            lambda name, cfg: fake,
        )
        runner = BackendRunner(backend_name="fake", config={})
        desc = runner.describe()
        for bit in (
            "streaming_deltas", "resumable", "interrupt", "approval_hooks",
            "parallel_sessions", "cost_reporting", "tool_filtering", "takeover",
        ):
            assert getattr(desc, bit) is True, f"describe() degraded {bit!r}"


class TestDescribeCostReporting:
    """When ``cost_reporting=True``, describe() must include pricing info."""

    def test_pricing_table_required_when_cost_reporting_true(
        self, monkeypatch,
    ) -> None:
        from orchestratord.backend_runner import BackendRunner

        fake = _fake_backend({"cost_reporting": True})
        monkeypatch.setattr(
            "orchestratord.backend_runner.resolve_backend",
            lambda name, cfg: fake,
        )
        runner = BackendRunner(backend_name="fake", config={})
        desc = runner.describe()
        # The Web UI uses this to draw cost-vs-token charts (§5.2.4).
        assert hasattr(desc, "model_pricing"), (
            "describe() lacks model_pricing — Web usage page cannot render"
        )
        assert desc.model_pricing is not None

    def test_pricing_optional_when_cost_reporting_false(
        self, stub_runner,
    ) -> None:
        desc = stub_runner.describe()
        if hasattr(desc, "model_pricing"):
            assert desc.model_pricing is None


class TestDescribeIsCacheable:
    """The descriptor must be JSON-serializable for the DB cache column."""

    def test_descriptor_serializes_to_json(self, stub_runner) -> None:
        import json

        desc = stub_runner.describe()
        try:
            payload = desc.model_dump()  # pydantic v2
        except AttributeError:
            payload = desc.__dict__
        encoded = json.dumps(payload, default=str)
        decoded = json.loads(encoded)
        assert "streaming_deltas" in decoded


class TestDescribeDoesNotInvokeCli:
    """``describe()`` MUST NOT shell out to the backend CLI."""

    def test_describe_runs_under_cli_guard(self, stub_runner) -> None:
        # If describe() ever shell out to ``codex`` / ``hermes`` /
        # ``opencode`` / ``dsh`` / ``clawcodex-dev``, the autouse
        # ``_backend_cli_guard_path`` fixture will cause the test to
        # fail with exit 126 before reaching this assertion.
        desc = stub_runner.describe()
        assert desc is not None


class TestDescribeSurvivesBackendVersionChange:
    """A version bump from the backend must invalidate the cache."""

    def test_describe_includes_backend_version(self, stub_runner) -> None:
        desc = stub_runner.describe()
        # The agent_capabilities_cache column is keyed on
        # (agent_id, backend_version); without the version field,
        # the cache could serve stale capability info across an
        # upgrade that flipped some bit.
        assert hasattr(desc, "backend_version")
        assert desc.backend_version == "1.0.0"
