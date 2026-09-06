"""Channel entity model (``docs/FEATURE_GAP_VS_MULTICA.md`` §7.5).

A channel is a notification-channel binding (Slack / Lark) for a workspace:
an in-channel ``@orchestratord <issue-id or natural language>`` mention
triggers issue dispatch, and session state transitions are pushed back to the
channel. DingTalk / WeCom / Telegram are deferred behind the same adapter
interface (``apps/web/app/{dingtalk,wecom,telegram}``).

Model invariant: ``provider`` ∈ {slack, lark} (the two OAuth integrations in
scope this phase); ``created_at`` is timezone-aware.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

_PROVIDERS = frozenset({"slack", "lark"})


def _normalize_created_at(value: datetime | None) -> datetime:
    """Return a tz-aware ``created_at``, matching the other entities."""
    if value is None:
        return datetime.now(UTC)
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


@dataclass
class Channel:
    id: UUID
    workspace_id: UUID
    provider: str
    name: str
    external_id: str
    created_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.provider not in _PROVIDERS:
            raise ValueError(
                f"invalid provider {self.provider!r}; "
                f"expected one of {sorted(_PROVIDERS)}"
            )
        self.created_at = _normalize_created_at(self.created_at)


__all__ = ["Channel"]
