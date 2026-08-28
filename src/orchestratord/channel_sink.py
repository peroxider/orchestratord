"""ChannelProgressSink — delivers formatted orchestrator events to the IM gateway.

An :class:`EventSink` (callable) that formats each :class:`OrchestratorEvent`
to IM text and hands it to a ``deliver`` callback. The callback bridges to
the gateway: in tests it records; in the orchestrator daemon it schedules
``gateway.send(OutboundMessage(...))`` on the running loop. Deliver is
exception-isolated so a gateway failure never breaks the orchestrator.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable

from .events.formatter import format_event
from .events.types import EventLevel, OrchestratorEvent

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
    except Exception:  # noqa: BLE001
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
        except Exception as exc:  # noqa: BLE001
            logger.exception("ChannelProgressSink deliver failed: %s", exc)


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


def build_ipc_deliver(im_client: Any) -> Callable[[OrchestratorEvent, str], None]:
    """Build a non-blocking event callback for an async IM IPC client."""
    loop = asyncio.get_running_loop()

    async def _send(text: str) -> None:
        try:
            await im_client.send_outbound(text)
        except Exception:  # noqa: BLE001
            logger.exception("channel IPC send failed")

    def _deliver(_event: OrchestratorEvent, text: str) -> None:
        loop.create_task(_send(text))

    return _deliver
