"""Stub inbound handler — binding guidance (v1 placeholder for default host agent).

The IM gateway's default host agent is intentionally not wired to a live
LLM in v1 (full agent execution is high-risk; only the contract + reply
hook shipped). Without ANY handler registered, ``InboundDispatcher`` drops
every authorized inbound message silently — the "message reaches
dispatcher but agent never replies" symptom.

This module registers a deterministic guidance stub: it replies with the
explicit REPL/orchestrator opt-in instruction through the outbound
dispatcher. That proves the full loop (dispatcher → handler → outbound →
channel adapter → WeChat) without implying a live default agent exists.

It is intentionally minimal: no LLM, no session state, no tool calls. When
a real default agent lands, replace the ``gateway.set_handler`` call in
the gateway daemon serve loop with it.
"""

from __future__ import annotations

import logging

from orchestratord.ipc.models import (
    AckLayer,
    AckReceipt,
    InboundMessage,
    OutboundMessage,
)

logger = logging.getLogger(__name__)


def make_stub_handler(outbound):
    """Return an ``InboundHandler`` that guides users to bind a live runtime.

    ``outbound`` is the gateway's :class:`OutboundDispatcher`. The handler
    resolves the reply target from the inbound message's channel + sender
    (``from_user_id``), so it works for any channel that carries those fields
    (WeChat iLink does).
    """

    async def _handle(message: InboundMessage) -> AckReceipt | None:
        target = message.from_user_id
        channel = message.channel
        if not target or not channel:
            logger.warning(
                "stub_agent: cannot reply — missing target/channel "
                "(channel=%r target=%r origin=%s)",
                channel,
                target,
                message.origin,
            )
            return AckReceipt(
                str(message.message_id or ""),
                AckLayer.ACCEPTED,
                message="stub: missing target/channel; no reply",
            )
        reply_text = (
            "当前会话尚未绑定任何运行时。orchestrator 侧请先启动 gateway，"
            "然后运行 orchestratord server connect-gateway 完成 opt-in 绑定。"
        )
        logger.info(
            "stub_agent reply: channel=%s target=%s text=%r",
            channel,
            target[:16] + "…" if len(target) > 16 else target,
            reply_text[:80],
        )
        try:
            result = await outbound.send(
                OutboundMessage(text=reply_text, channel=channel, target=target, markdown=False)
            )
        except Exception:
            logger.exception("stub_agent: outbound send failed")
            return AckReceipt(
                str(message.message_id or ""),
                AckLayer.ACCEPTED,
                message="stub: outbound send error",
            )
        if result.ok:
            return AckReceipt(
                str(message.message_id or ""),
                AckLayer.PROCESSED,
                message="stub: replied",
            )
        logger.warning(
            "stub_agent: outbound send returned non-ok: %s",
            getattr(result, "message", result),
        )
        return AckReceipt(
            str(message.message_id or ""),
            AckLayer.ACCEPTED,
            message=f"stub: send failed ({getattr(result, 'status', '?')})",
        )

    return _handle


__all__ = ["make_stub_handler"]
