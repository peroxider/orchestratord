"""Integration entity model (``docs/FEATURE_GAP_VS_MULTICA.md`` §7.5).

An integration is the persistent side of a workspace's notification-channel
OAuth handshake: one Slack (or Lark) app installation per workspace, holding
the inbound-webhook URL the adapters POST to. It is distinct from a
:class:`orchestratord.domain.channel.Channel`, which is one of N channel
bindings *within* that integration (the specific Slack channel / Lark chat the
bot writes to).

Model invariant: ``provider`` ∈ {slack, lark} (the two OAuth integrations in
scope this phase; DingTalk / WeCom / Telegram are deferred behind the same
adapter interface). ``webhook_url`` must be non-empty; ``created_at`` is
timezone-aware.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

_PROVIDERS = frozenset({"slack", "lark"})


def _normalize_created_at(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(UTC)
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


@dataclass
class Integration:
    id: UUID
    workspace_id: UUID
    provider: str
    webhook_url: str
    created_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.provider not in _PROVIDERS:
            raise ValueError(
                f"invalid provider {self.provider!r}; "
                f"expected one of {sorted(_PROVIDERS)}"
            )
        if not self.webhook_url.strip():
            raise ValueError("webhook_url must not be empty")
        self.created_at = _normalize_created_at(self.created_at)


__all__ = ["Integration"]
