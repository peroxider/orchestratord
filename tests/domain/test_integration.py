"""Integration entity invariants (§7.5).

An integration is the persistent side of a Slack / Lark OAuth handshake.
Invariants:

* ``provider`` ∈ {slack, lark}.
* ``webhook_url`` is non-empty.
* ``created_at`` is timezone-aware.

Reference: docs/FEATURE_GAP_VS_MULTICA.md §7.5.
"""
from __future__ import annotations

from uuid import uuid4

import pytest

from orchestratord.domain.integration import Integration


def _integration(**overrides) -> Integration:
    defaults = {
        "id": uuid4(),
        "workspace_id": uuid4(),
        "provider": "slack",
        "webhook_url": "https://hooks.slack.com/services/T/B/X",
    }
    defaults.update(overrides)
    return Integration(**defaults)


class TestIntegrationFields:
    def test_required_fields_present(self) -> None:
        inst = _integration()
        assert inst.provider == "slack"
        assert inst.webhook_url.startswith("https://")

    def test_created_at_is_timezone_aware(self) -> None:
        assert _integration().created_at.tzinfo is not None


class TestProviderWhitelist:
    def test_all_providers_accepted(self) -> None:
        for provider in ("slack", "lark"):
            assert _integration(provider=provider).provider == provider

    def test_unknown_provider_rejected(self) -> None:
        with pytest.raises(ValueError, match="provider"):
            _integration(provider="telegram")


class TestWebhookUrl:
    def test_empty_webhook_url_rejected(self) -> None:
        with pytest.raises(ValueError, match="webhook_url"):
            _integration(webhook_url="   ")

    def test_padded_url_not_rejected(self) -> None:
        # The domain only enforces "non-empty after strip"; the API layer strips.
        inst = _integration(webhook_url="  https://example.com/hook  ")
        assert inst.webhook_url.strip() == "https://example.com/hook"
