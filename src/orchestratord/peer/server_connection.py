"""Server-side wrapper for one inbound ``/peer/v1/stream`` connection (PR-B2).

Each HTTP chunked-POST request owns exactly one
:class:`PeerServerConnection`. The connection carries:

* the authenticated :class:`orchestratord.db.models.peer.Peer` row from
  :func:`orchestratord.api.deps.require_peer_auth`,
* an outbound :class:`asyncio.Queue` the frame router drains into the
  HTTP ``StreamingResponse`` (PUSH frames the server emits proactively:
  EVENT, RESULT, PONG, WELCOME),
* an ``in_flight`` counter incremented while an INVOKE is being
  processed and decremented when its RESULT is queued — D24 drain uses
  this to decide whether the GOODBYE/close can complete immediately or
  must wait,
* a :meth:`aclose` path that puts the queue sentinel and marks the
  connection closed so the writer loop exits.

The connection is push-only on the server side (the writer reads from
its outbound queue; the reader puts inbound frames into the dispatcher).
It does not implement the request/response :class:`FrameTransport`
Protocol used by the client — the protocol is one-directional per peer
direction.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from orchestratord.peer.protocol import PeerFrame

if TYPE_CHECKING:  # pragma: no cover - import cycle guard for type checkers
    from orchestratord.db.models.peer import Peer

logger = logging.getLogger(__name__)

# Server-side outbound queue cap (PR-B2). One queue per inbound
# connection keeps memory bounded — a slow consumer back-pressures into
# ``send_frame`` rather than letting frames pile up to OOM.
OUTBOUND_QUEUE_MAXSIZE = 256


class PeerServerConnection:
    """One inbound ``POST /peer/v1/stream`` session (DESIGN §6.1, PR-B2)."""

    def __init__(
        self,
        *,
        peer: Peer,
        outbound_maxsize: int = OUTBOUND_QUEUE_MAXSIZE,
    ) -> None:
        self._peer = peer
        self._outbound: asyncio.Queue[PeerFrame | None] = asyncio.Queue(
            maxsize=outbound_maxsize
        )
        self._in_flight = 0
        self._closed = False
        self.remote_orch_id = peer.orch_id

    # -- properties --

    @property
    def peer(self) -> Peer:
        """The accepted registry row (already authenticated)."""
        return self._peer

    @property
    def in_flight(self) -> int:
        """Outstanding INVOKEs whose RESULT has not been queued yet (D24)."""
        return self._in_flight

    @property
    def closed(self) -> bool:
        return self._closed

    # -- inbound (router reads frames and feeds them into the dispatcher) --

    def entered(self) -> None:
        """Mark one INVOKE as in-flight before dispatch (D24 accounting)."""
        self._in_flight += 1

    def released(self) -> None:
        """Mark one INVOKE as done after the RESULT is queued."""
        if self._in_flight > 0:
            self._in_flight -= 1

    # -- outbound (router pushes server-initiated frames into the queue) --

    async def send_frame(self, frame: PeerFrame) -> None:
        """Queue one frame for the writer loop to push to the HTTP body.

        ``None`` is the sentinel the writer loop treats as "close the
        response stream"; :meth:`aclose` is the only path that sends it.
        """
        if self._closed:
            return
        await self._outbound.put(frame)

    async def next_outbound(self) -> PeerFrame | None:
        """Block for the next frame; ``None`` means the writer must stop."""
        return await self._outbound.get()

    # -- lifecycle ---

    async def aclose(self) -> None:
        """Close the queue. Idempotent; safe to call from any cleanup path."""
        if self._closed:
            return
        self._closed = True
        try:
            self._outbound.put_nowait(None)
        except asyncio.QueueFull:
            # If the queue is saturated we still need to terminate the
            # writer loop — drop the sentinel in via ``put`` after the
            # queue has space.
            await self._outbound.put(None)
        logger.debug(
            "peer server connection closed (orch_id=%s, in_flight=%d)",
            self._peer.orch_id,
            self._in_flight,
        )