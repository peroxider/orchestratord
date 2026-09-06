"""Notification-service dependency for the FastAPI routers (§7.5).

Builds the default ``NotificationService`` (Slack + Lark adapters over httpx)
as a lazily-created singleton. Tests override ``get_notification_service`` with
a fake to assert delivery without hitting a real provider.
"""

from __future__ import annotations

from orchestratord.notifications import (
    LarkAdapter,
    NotificationService,
    SlackAdapter,
)

_default: NotificationService | None = None


def get_notification_service() -> NotificationService:
    global _default
    if _default is None:
        _default = NotificationService(
            {"slack": SlackAdapter(), "lark": LarkAdapter()}
        )
    return _default


__all__ = ["get_notification_service"]
