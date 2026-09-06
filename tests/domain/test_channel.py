"""Channel entity invariants (§7.5).

A channel is a Slack / Lark notification binding. Invariant:

* ``provider`` ∈ {slack, lark}.
* ``created_at`` is timezone-aware.

Reference: docs/FEATURE_GAP_VS_MULTICA.md §7.5.
"""
from __future__ import annotations

from uuid import uuid4

import pytest

from orchestratord.domain.channel import Channel


def _channel(**overrides) -> Channel:
    defaults = {
        "id": uuid4(),
        "workspace_id": uuid4(),
        "provider": "slack",
        "name": "eng",
        "external_id": "C123",
    }
    defaults.update(overrides)
    return Channel(**defaults)


class TestChannelFields:
    def test_required_fields_present(self) -> None:
        ch = _channel()
        assert ch.provider == "slack"
        assert ch.name == "eng"
        assert ch.external_id == "C123"

    def test_created_at_is_timezone_aware(self) -> None:
        assert _channel().created_at.tzinfo is not None


class TestProviderWhitelist:
    def test_all_providers_accepted(self) -> None:
        for provider in ("slack", "lark"):
            assert _channel(provider=provider).provider == provider

    def test_unknown_provider_rejected(self) -> None:
        with pytest.raises(ValueError, match="provider"):
            _channel(provider="telegram")
