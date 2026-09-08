"""Notification-channel adapters (§7.5).

Provider-specific delivery for the ``channels`` / ``integrations`` surface.
Slack and Lark are the two OAuth integrations in scope this phase; DingTalk /
WeCom / Telegram are deferred behind the same :class:`NotificationAdapter`
protocol.

Importers/callers: ``orchestratord.api.notifications`` builds the default
``NotificationService``; ``orchestratord.api.routers.channels`` calls
``NotificationService.deliver`` on a session-state push.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol

import httpx


class NotificationAdapter(Protocol):
    """A provider's outbound delivery primitive (extensible to DingTalk/etc.)."""

    provider: str

    async def send(self, *, webhook_url: str, text: str) -> None:
        """POST ``text`` to the provider's inbound webhook."""


class _WebhookAdapter:
    """Shared HTTP delivery: POST a provider-shaped JSON body to a webhook."""

    provider: str = ""

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._client = client

    def _payload(self, text: str) -> dict:
        raise NotImplementedError

    async def send(self, *, webhook_url: str, text: str) -> None:
        if self._client is not None:
            resp = await self._client.post(webhook_url, json=self._payload(text))
            resp.raise_for_status()
            return
        async with httpx.AsyncClient() as client:
            resp = await client.post(webhook_url, json=self._payload(text))
            resp.raise_for_status()


class SlackAdapter(_WebhookAdapter):
    provider = "slack"

    def _payload(self, text: str) -> dict:
        return {"text": text}


class LarkAdapter(_WebhookAdapter):
    provider = "lark"

    def _payload(self, text: str) -> dict:
        return {"msg_type": "text", "content": {"text": text}}


class NotificationService:
    """Resolve a provider adapter and deliver a best-effort notification."""

    def __init__(self, adapters: Mapping[str, NotificationAdapter]) -> None:
        self._adapters = dict(adapters)

    async def deliver(
        self, *, provider: str, webhook_url: str, text: str
    ) -> bool:
        """Return ``True`` when delivered; ``False`` when no adapter / failed.

        Delivery is best-effort: a provider outage must not break the caller
        (the session-state push is a fire-and-forget notification).
        """
        adapter = self._adapters.get(provider)
        if adapter is None:
            return False
        try:
            await adapter.send(webhook_url=webhook_url, text=text)
        except httpx.HTTPError:
            return False
        return True


__all__ = [
    "LarkAdapter",
    "NotificationAdapter",
    "NotificationService",
    "SlackAdapter",
]
