"""Recipient authorization shared by gateway delivery and origin admission."""

from __future__ import annotations

from typing import Any

from .capabilities import ChannelCapability


def authorized_recipients(adapter: Any) -> frozenset[str]:
    """Read the current allowlist; missing or broken contracts fail closed."""
    get = getattr(adapter, "authorized_recipients", None)
    if not callable(get):
        return frozenset()
    try:
        return frozenset(user for user in get() if isinstance(user, str) and user)
    except Exception:  # noqa: BLE001 — adapter contract failures must deny delivery
        return frozenset()


def permits_target(adapter: Any, target: str | None) -> bool:
    """Authorize private-message recipients, preserving configured webhooks."""
    caps = adapter.capabilities
    if callable(getattr(adapter, "authorized_recipients", None)) or any(
        caps.has(cap)
        for cap in (
            ChannelCapability.INBOUND_POLLING,
            ChannelCapability.INBOUND_WEBHOOK,
        )
    ):
        return bool(target) and target in authorized_recipients(adapter)
    return True
