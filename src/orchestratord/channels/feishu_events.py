"""Translate SDK inbound events into gateway ``InboundMessage``.

``lark_oapi.channel.FeishuChannel`` already deserializes the WebSocket event,
deduplicates, parses text/post content into ``content_text`` and drops
self-echo (``InboundConfig.drop_self_sent``). This module is the thin
bridge from the SDK's :class:`~lark_oapi.channel.types.InboundMessage` to the
gateway's :class:`InboundMessage`, applying the V1 p2p-only /
fail-closed allowlist admission on the way: an empty
``allowed_user_open_id`` rejects ALL inbound messages.
"""

from __future__ import annotations

import logging
from typing import Any

from .feishu_settings import FeishuAppSettings

logger = logging.getLogger(__name__)

_empty_allowlist_warned = False


def _warn_empty_allowlist_once() -> None:
    """Warn (once per process) that an empty allowlist fails closed."""
    global _empty_allowlist_warned
    if _empty_allowlist_warned:
        return
    _empty_allowlist_warned = True
    logger.warning(
        "feishu inbound rejected: allowed_user_open_id is not configured; "
        "all inbound messages will be dropped (fail closed). Configure the "
        "authorized operator open_id in extra.allowed_user_open_id in channels.yaml."
    )


def translate_inbound(inbound: Any, settings: FeishuAppSettings) -> Any | None:
    """Map an SDK ``InboundMessage`` to a gateway ``InboundMessage``.

    Returns ``None`` when the event is dropped (non-p2p, disallowed sender,
    empty text, missing ids). ``inbound`` is duck-typed so tests can pass a
    lightweight stand-in.
    """
    chat_type = str(_get(_get(inbound, "conversation"), "chat_type") or "").lower()
    if chat_type not in {"p2p", "private"}:
        logger.debug("feishu event dropped: not p2p (chat_type=%s)", chat_type)
        return None
    open_id = str(_get(_get(inbound, "sender"), "open_id") or "")
    if not open_id:
        logger.debug("feishu event dropped: no sender open_id")
        return None
    # SDK ``drop_self_sent`` already filters bot echo, but keep a defensive
    # check in case bot_open_id was resolved late / overridden in config.
    if settings.bot_open_id and open_id == settings.bot_open_id:
        return None
    # Sender authorization fails closed: an empty allowlist rejects every
    # p2p sender instead of admitting all private-chat users.
    if not settings.allowed_user_open_id:
        _warn_empty_allowlist_once()
        logger.debug("feishu event dropped: no allowed_user_open_id configured (fail closed)")
        return None
    if open_id != settings.allowed_user_open_id:
        logger.debug("feishu event dropped: sender not in allowlist: %s", open_id[:16])
        return None
    text = str(_get(inbound, "content_text") or "").strip()
    if not text:
        logger.debug(
            "feishu event dropped: empty text (message_id=%s)",
            str(_get(inbound, "id") or "")[:16],
        )
        return None
    message_id = str(_get(inbound, "id") or "")
    chat_id = str(_get(_get(inbound, "conversation"), "chat_id") or "")
    if not message_id or not chat_id:
        logger.debug("feishu event dropped: missing message_id/chat_id")
        return None
    from orchestratord.ipc.models import InboundMessage

    logger.info(
        "feishu inbound normalized: from=%s msg_id=%s chat=%s len=%d",
        open_id[:16],
        message_id[:16],
        chat_id[:16],
        len(text),
    )
    return InboundMessage(
        origin=f"feishu:dm:{settings.app_id}:{open_id}",
        text=text,
        message_id=message_id,
        channel=settings.channel_id,
        context_token=chat_id,
        from_user_id=open_id,
        raw={
            "message_id": message_id,
            "chat_id": chat_id,
            "open_id": open_id,
            "create_time": _get(inbound, "create_time"),
            "raw_content_type": _get(inbound, "raw_content_type"),
        },
    )


def _get(obj: Any, key: str, default: Any = None) -> Any:
    """Attribute-or-key accessor bridging SDK model objects and raw dicts."""
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


__all__ = ["translate_inbound"]
