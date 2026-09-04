"""ChannelProgressSink — delivers formatted orchestrator events to the IM gateway.

An :class:`EventSink` (callable) that formats each :class:`OrchestratorEvent`
to IM text and hands it to a ``deliver`` callback. The callback bridges to
the gateway: in tests it records; in the orchestrator daemon it schedules
``gateway.send(OutboundMessage(...))`` on the running loop. Deliver is
exception-isolated so a gateway failure never breaks the orchestrator.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Callable
from typing import Any

from ..events.formatter import format_event
from ..events.types import EventLevel, OrchestratorEvent

logger = logging.getLogger(__name__)

# Map event level → outbound message level string understood by the gateway.
_LEVEL_MAP = {
    EventLevel.SUCCESS: "success",
    EventLevel.INFO: "info",
    EventLevel.WARN: "warn",
    EventLevel.ERROR: "error",
}


async def _safe_send(gateway: Any, message: Any) -> None:
    """Isolate asynchronous gateway failures from the orchestrator task."""
    try:
        await gateway.send(message)
    except Exception:
        logger.exception("channel gateway send failed")


class ChannelProgressSink:
    """EventSink that formats + delivers events to the gateway."""

    def __init__(
        self,
        deliver: Callable[[OrchestratorEvent, str], None],
    ) -> None:
        self._deliver = deliver
        self.events: list[OrchestratorEvent] = []

    def __call__(self, event: OrchestratorEvent) -> None:
        text = format_event(event)
        self.events.append(event)
        try:
            self._deliver(event, text)
        except Exception:
            logger.exception("ChannelProgressSink deliver failed")


def build_gateway_deliver(
    gateway,
    channel: str,
    *,
    loop: asyncio.AbstractEventLoop | None = None,
) -> Callable[[OrchestratorEvent, str], None]:
    """Build a sync ``deliver`` that schedules an async ``gateway.send``.

    Used when the orchestrator runs in-process with the gateway (tests, or a
    future single-process mode). If no event loop is running, the event is
    dropped with a warning (the orchestrator must not block on IM).
    """
    from orchestratord.ipc.models import OutboundMessage

    def _deliver(event: OrchestratorEvent, text: str) -> None:
        msg = OutboundMessage(
            text=text,
            channel=channel,
            level=_LEVEL_MAP.get(event.level, "info"),
            markdown=True,
            semantic_tags=[event.event_type],
            metadata={"issue_id": event.issue_id, "event_type": event.event_type},
        )
        try:
            lp = loop or asyncio.get_event_loop()
        except RuntimeError:
            logger.warning("no running loop; dropping IM event %s", event.event_type)
            return
        lp.create_task(_safe_send(gateway, msg))

    return _deliver


def event_outbound_metadata(event: OrchestratorEvent) -> dict[str, Any]:
    """Build the outbound metadata envelope for an orchestrator event.

    SPEC im-gateway Phase 4: events sent over the gateway carry
    ``issue_id`` / ``event_type`` / ``level`` / ``markdown`` so channels can
    render them (e.g. Feishu markdown cards) without re-parsing the text.
    """
    return {
        "issue_id": event.issue_id,
        "event_type": event.event_type,
        "level": _LEVEL_MAP.get(event.level, "info"),
        "markdown": True,
    }


def _accepts_param(method: Callable[..., Any], param: str) -> bool:
    """Whether ``method``'s signature accepts ``param``.

    Older IM clients implement ``send_outbound(text)`` only; the metadata
    envelope is passed when the signature supports it so both generations
    of embedders keep working.
    """
    try:
        sig = inspect.signature(method)
    except (TypeError, ValueError):
        return False
    if param in sig.parameters:
        return True
    return any(p.kind is inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values())


def deliver_event_via_client(
    im_client: Any,
    event: OrchestratorEvent,
    text: str,
    *,
    loop: asyncio.AbstractEventLoop | None = None,
) -> None:
    """Schedule ``im_client.send_outbound(text)`` with the event metadata.

    Non-blocking and exception-isolated: IM delivery must never break the
    orchestrator loop. The metadata envelope is forwarded when the client's
    ``send_outbound`` accepts it.
    """
    if loop is None:
        loop = asyncio.get_event_loop()

    async def _send() -> None:
        try:
            if _accepts_param(im_client.send_outbound, "metadata"):
                await im_client.send_outbound(text, metadata=event_outbound_metadata(event))
            else:
                await im_client.send_outbound(text)
        except Exception:
            logger.exception("channel IPC send failed")

    loop.create_task(_send())


def build_ipc_deliver(im_client: Any) -> Callable[[OrchestratorEvent, str], None]:
    """Build a non-blocking event callback for an async IM IPC client."""
    loop = asyncio.get_running_loop()

    def _deliver(event: OrchestratorEvent, text: str) -> None:
        deliver_event_via_client(im_client, event, text, loop=loop)

    return _deliver
