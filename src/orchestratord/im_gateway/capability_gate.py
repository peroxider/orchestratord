"""Capability gate — fail-closed check before a gateway calls a channel.

Every gateway→channel call goes through :meth:`CapabilityGate.require`
so an undeclared capability (e.g. sending media over a text-only
channel) is rejected before any platform API is touched.
"""

from __future__ import annotations

from orchestratord.channels.capabilities import (
    ChannelAdapter,
    ChannelCapability,
)


class CapabilityGate:
    def __init__(self, registry) -> None:  # registry: ChannelAdapterRegistry
        self._registry = registry

    def require(
        self,
        channel: str | ChannelAdapter,
        capability: ChannelCapability,
    ) -> ChannelAdapter:
        """Resolve ``channel`` and fail closed if ``capability`` is undeclared."""
        return self._registry.require_capability(channel, capability)

    def require_outbound(self, channel: str | ChannelAdapter) -> ChannelAdapter:
        return self.require(channel, ChannelCapability.OUTBOUND_TEXT)

    def require_context_reply(self, channel: str | ChannelAdapter) -> ChannelAdapter:
        adapter = self.require(channel, ChannelCapability.CONTEXT_REPLY)
        return adapter

    def require_media(
        self, channel: str | ChannelAdapter, capability: ChannelCapability
    ) -> ChannelAdapter:
        if capability not in (
            ChannelCapability.MEDIA_IMAGE,
            ChannelCapability.MEDIA_FILE,
            ChannelCapability.MEDIA_VIDEO,
        ):
            raise ValueError(f"{capability!r} is not a media capability")
        return self.require(channel, capability)


__all__ = ["CapabilityGate"]
