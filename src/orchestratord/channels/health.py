"""Exact channel readiness states used by daemon startup and reload."""

from __future__ import annotations

from .results import ChannelHealth


def channel_status_ready(status: object) -> bool:
    return status in {"connected", "logged_in", "websocket:connected"}


def channel_health_ready(health: ChannelHealth | None) -> bool:
    return bool(
        health is not None
        and health.healthy
        and (not health.account_status or channel_status_ready(health.account_status))
    )
