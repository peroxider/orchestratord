"""Origin resolution helpers shared by the IPC server and the gateway."""

from __future__ import annotations

from typing import Any

from orchestratord.ipc.models import (
    FEISHU_DM_ALL_ORIGIN,
    IM_DIRECT_ALL_ORIGIN,
    WECHAT_DIRECT_ALL_ORIGIN,
)


def resolve_origin(origin: str, gateway=None) -> tuple[str | None, str | None]:
    """Map an IM origin key to ``(channel_name, target_user_id)`` for outbound.

    ``wechat:direct:{account}:{user}`` → the registered WeChat channel name
    + the user id. The channel name is resolved from the gateway config's
    unique WeChat entry first, then from adapter config if available, then
    from the v1 default name.

    The wildcard ``wechat:direct:*:*`` is resolved here to a concrete
    sender so opt-in hosts (orchestrator/REPL) can emit OUTBOUND without
    knowing the operator's WeChat user id. The WeChat adapter is the single
    source: its ``last_known_sender`` returns the most recent real inbound
    sender (in-memory, current lifetime), falling back to its persisted
    context-token users (survives a gateway restart with no new inbound).
    If none is known, returns ``(None, None)`` and the caller NACKs — an
    operator who has never messaged genuinely cannot be addressed.
    """
    parts = origin.split(":")
    if origin == IM_DIRECT_ALL_ORIGIN:
        return resolve_last_known_im_sender(gateway)
    if is_concrete_wechat_direct_origin(origin):
        target = parts[3]
        channel = configured_wechat_channel(gateway)
        if channel is not None:
            return channel, target
        adapter = wechat_adapter(gateway)
        if adapter is not None:
            return adapter.channel_id, target
        return "wechat", target
    if parts[:2] == ["wechat", "direct"] and origin == WECHAT_DIRECT_ALL_ORIGIN:
        channel, user = _last_known_sender_for_type(gateway, "wechat")
        if channel is not None and user is not None:
            return channel, user
        return None, None
    if len(parts) >= 4 and parts[0] == "feishu" and parts[1] == "dm":
        if origin == FEISHU_DM_ALL_ORIGIN or parts[3] == "*":
            return _last_known_sender_for_type(gateway, "feishu")
        channel = configured_channel_by_type(gateway, "feishu") or "feishu"
        return channel, parts[3]
    return None, None


def resolve_last_known_im_sender(gateway: Any) -> tuple[str | None, str | None]:
    """Return the first known sender from any supported IM adapter."""
    for channel_type in ("wechat", "feishu"):
        channel, target = _last_known_sender_for_type(gateway, channel_type)
        if channel and target:
            return channel, target
    return None, None


def _last_known_sender_for_type(gateway: Any, channel_type: str) -> tuple[str | None, str | None]:
    adapter = adapter_by_channel_type(gateway, channel_type)
    if adapter is None:
        return None, None
    last_known = getattr(adapter, "last_known_sender", None)
    target = last_known() if callable(last_known) else None
    if not target:
        return None, None
    channel = configured_channel_by_type(gateway, channel_type) or getattr(
        adapter, "channel_id", None
    )
    return channel, target


def wechat_adapter(gateway: Any):
    """Return the gateway's registered WeChat adapter, or None."""
    return adapter_by_channel_type(gateway, "wechat")


def adapter_by_channel_type(gateway: Any, channel_type: str):
    """Return the gateway's registered adapter for a channel type, or None."""
    registry = getattr(gateway, "registry", None)
    if registry is None or not hasattr(registry, "all_adapters"):
        return None
    wanted = str(channel_type).lower()
    for adapter in registry.all_adapters():
        cfg = getattr(adapter, "config", None) or getattr(adapter, "_config", None)
        actual = getattr(cfg, "type", None)
        actual_value = getattr(actual, "value", actual)
        if str(actual_value).lower() == wanted:
            return adapter
    return None


def configured_wechat_channel(gateway: Any) -> str | None:
    """Return the configured WeChat channel name from gateway config, or None."""
    return configured_channel_by_type(gateway, "wechat")


def configured_channel_by_type(gateway: Any, channel_type: str) -> str | None:
    """Return a configured channel name by type from gateway config, or None."""
    config = getattr(gateway, "config", None)
    get_by_type = getattr(config, "get_channel_by_type", None)
    if not callable(get_by_type):
        return None
    try:
        channel = get_by_type(channel_type)
    except Exception:  # noqa: BLE001
        return None
    return getattr(channel, "name", None) if channel is not None else None


def is_concrete_wechat_direct_origin(origin: str) -> bool:
    """True for ``wechat:direct:{account}:{user}`` with non-wildcard fields."""
    parts = origin.split(":")
    return (
        len(parts) >= 4
        and parts[0] == "wechat"
        and parts[1] == "direct"
        and parts[2] not in ("", "*")
        and parts[3] not in ("", "*")
    )


__all__ = [
    "adapter_by_channel_type",
    "configured_channel_by_type",
    "configured_wechat_channel",
    "is_concrete_wechat_direct_origin",
    "resolve_last_known_im_sender",
    "resolve_origin",
    "wechat_adapter",
]
