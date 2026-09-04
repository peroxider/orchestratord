"""Origin resolution helpers shared by the IPC server and the gateway."""

from __future__ import annotations

import logging
from typing import Any

from orchestratord.ipc.models import (
    FEISHU_DM_ALL_ORIGIN,
    IM_DIRECT_ALL_ORIGIN,
    WECHAT_DIRECT_ALL_ORIGIN,
)

logger = logging.getLogger(__name__)


def resolve_origin(origin: str, gateway=None) -> tuple[str | None, str | None]:
    """Map an IM origin key to ``(channel_name, target_user_id)`` for outbound.

    ``wechat:direct:{account}:{user}`` → the registered WeChat channel name
    + the user id. The channel name is resolved from the gateway config's
    unique WeChat entry first, then from adapter config if available, then
    from the v1 default name. Concrete origins always resolve to their
    explicit user — wildcard authorization rules never override them.

    Wildcard origins (``wechat:direct:*:*``, ``feishu:dm:*:*``,
    ``im:direct:*:*``) let opt-in hosts (orchestrator/REPL) emit OUTBOUND
    without knowing the operator's user id. They resolve through
    :func:`wildcard_recipient`: when the channel's adapter exposes
    ``authorized_recipients()``, the wildcard resolves ONLY when exactly one
    recipient is authorized — 0 or 2+ authorized users return ``(None, None)``
    and the caller NACKs, so one operator's reports can never leak to
    another operator in multi-user setups. Adapters without the method keep
    the legacy ``last_known_sender`` fallback (see :func:`wildcard_recipient`
    for the transitional-compatibility rationale).
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
        return wildcard_recipient(gateway, "wechat")
    if len(parts) >= 4 and parts[0] == "feishu" and parts[1] == "dm":
        if origin == FEISHU_DM_ALL_ORIGIN or parts[3] == "*":
            return wildcard_recipient(gateway, "feishu")
        channel = configured_channel_by_type(gateway, "feishu") or "feishu"
        return channel, parts[3]
    return None, None


def resolve_last_known_im_sender(gateway: Any) -> tuple[str | None, str | None]:
    """Resolve ``im:direct:*:*`` to the single authorized IM recipient.

    Tries WeChat then Feishu; each channel resolves only through the
    wildcard rules in :func:`wildcard_recipient` (unique-authorized-recipient
    first, legacy last-known-sender fallback second).
    """
    for channel_type in ("wechat", "feishu"):
        channel, target = wildcard_recipient(gateway, channel_type)
        if channel and target:
            return channel, target
    return None, None


def resolve_report_targets(origin: str, gateway: Any) -> list[tuple[str, str | None]]:
    """Resolve an OUTBOUND origin to one or more ``(channel, target)`` pairs.

    Event-report fan-out (review P2): when ``origin`` is the wildcard
    ``im:direct:*:*`` and the gateway config declares explicit
    ``report_targets``, each configured target is resolved independently
    (concrete origins, per-channel wildcards, or bare channel names for
    target-less webhook push), and the results are returned as a list —
    one send per target. Unresolvable entries are skipped with a log line;
    an empty list means the caller NACKs.

    Without configured ``report_targets`` (the default), the wildcard
    keeps the single-authorized-recipient resolution
    (:func:`resolve_last_known_im_sender`), and every other origin
    resolves exactly like :func:`resolve_origin` — one entry at most.
    """
    if origin == IM_DIRECT_ALL_ORIGIN:
        configured = _configured_report_targets(gateway)
        if configured:
            resolved: list[tuple[str, str | None]] = []
            seen: set[tuple[str, str | None]] = set()
            for target_origin in configured:
                pair = _resolve_report_target(target_origin, gateway)
                if pair is None:
                    logger.warning(
                        "report target %r is not resolvable; skipping", target_origin
                    )
                    continue
                if pair in seen:
                    continue
                seen.add(pair)
                resolved.append(pair)
            return resolved
    channel, target = resolve_origin(origin, gateway)
    if channel is None:
        return []
    return [(channel, target)]


def _configured_report_targets(gateway: Any) -> list[str]:
    config = getattr(gateway, "config", None)
    targets = getattr(config, "report_targets", None)
    return [str(t) for t in (targets or []) if t]


def _resolve_report_target(target_origin: str, gateway: Any) -> tuple[str, str | None] | None:
    """Resolve one configured report target to ``(channel, target | None)``.

    A bare channel name (no ``:``) addresses a webhook channel directly —
    webhook push has no per-user target, so the target is ``None``. Anything
    else goes through :func:`resolve_origin`.
    """
    if ":" not in target_origin:
        registry = getattr(gateway, "registry", None)
        get = getattr(registry, "get", None)
        if callable(get) and get(target_origin) is not None:
            return target_origin, None
        return None
    channel, target = resolve_origin(target_origin, gateway)
    if channel is None:
        return None
    return channel, target


def wildcard_recipient(gateway: Any, channel_type: str) -> tuple[str | None, str | None]:
    """Resolve a wildcard origin for ``channel_type`` to a concrete target.

    New rule: when the channel's adapter exposes ``authorized_recipients()``
    (returning ``list[str]``; empty = nobody authorized), the wildcard
    resolves ONLY when exactly one recipient is authorized. With 0 or 2+
    authorized users there is no single safe target, so this returns
    ``(None, None)`` and the caller NACKs — a global "last sender" would
    otherwise route one operator's event reports to whoever messaged most
    recently.

    Transitional compatibility: adapters that do NOT expose the method
    (webhook adapters, test fakes, and real adapters that have not shipped
    ``authorized_recipients`` yet) keep the legacy ``last_known_sender``
    fallback so their behavior is unchanged until every real inbound adapter
    ships the method.
    """
    adapter = adapter_by_channel_type(gateway, channel_type)
    if adapter is None:
        return None, None
    authorized = getattr(adapter, "authorized_recipients", None)
    if callable(authorized):
        try:
            recipients = [r for r in (authorized() or []) if r]
        except Exception:  # noqa: BLE001 — a broken adapter must not crash resolution
            recipients = []
        if len(recipients) == 1:
            channel = (
                configured_channel_by_type(gateway, channel_type)
                or getattr(adapter, "channel_id", None)
            )
            return channel, recipients[0]
        return None, None
    return _last_known_sender_for_type(gateway, channel_type)


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
    "resolve_report_targets",
    "wechat_adapter",
    "wildcard_recipient",
]
