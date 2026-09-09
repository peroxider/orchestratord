"""Live peer-connection registry — outbound clients + inbound servers (D24).

Phase 1 created ``PeerClient`` connections on demand; PR-B2 adds the
mirrored inbound side (``PeerServerConnection`` per
``POST /peer/v1/stream`` request). This module is the daemon-side
handle on whichever connections are currently open so two behaviors
can find them:

* **D24 shutdown drain** — :func:`shutdown_peer_connections` waits up to
  five seconds for in-flight requests to drain, then sends each peer a
  GOODBYE frame carrying the final in-flight count. ``cli/serve.py``
  invokes it from the uvicorn shutdown path (SIGTERM/SIGINT both land
  there). Inbound connections have their outbound queue sentineled so
  the writer loop exits cleanly.
* **§6.2 Phase-B bridge hook** — the sessions router's
  ``_forward_approval``/``_forward_interrupt`` no-op branches can forward
  to a reachable peer when one is connected (capability-gated; inert in
  Phase 1).

The registry is process-local by design: connections die with the
process, so there is nothing to persist or reconcile across restarts.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - import cycle guard for type checkers
    from orchestratord.peer.client import PeerClient
    from orchestratord.peer.server_connection import PeerServerConnection

logger = logging.getLogger(__name__)

_DEFAULT_DRAIN_SECONDS = 5.0

_live: set[PeerClient] = set()
# PR-B2: inbound ``/peer/v1/stream`` connections. The frame router
# registers a wrapper per HTTP request after ``require_peer_auth``
# succeeds; the same wrapper is unregistered in the finally path.
_inbound: set[PeerServerConnection] = set()


# -- outbound (PeerClient) --


def register_client(client: PeerClient) -> None:
    """Track *client* after a successful handshake (PeerClient.open)."""
    _live.add(client)


def unregister_client(client: PeerClient) -> None:
    """Forget *client* once its session is torn down (PeerClient.close)."""
    _live.discard(client)


def live_clients() -> list[PeerClient]:
    """Snapshot of the currently open peer connections."""
    return list(_live)


# -- inbound (PeerServerConnection, PR-B2) --


def register_inbound(conn: PeerServerConnection) -> None:
    """Track an inbound frame-stream connection after ``require_peer_auth``."""
    _inbound.add(conn)


def unregister_inbound(conn: PeerServerConnection) -> None:
    """Forget an inbound connection after its request finishes or errors."""
    _inbound.discard(conn)


def live_inbound() -> list[PeerServerConnection]:
    """Snapshot of the currently open inbound frame-stream connections."""
    return list(_inbound)


# -- shutdown drain --


async def shutdown_peer_connections(
    drain_seconds: float = _DEFAULT_DRAIN_SECONDS,
) -> int:
    """D24: drain in-flight work, then close every peer connection.

    Walks both directions (outbound ``PeerClient`` + inbound
    ``PeerServerConnection``), waits up to *drain_seconds* **in total**
    (one shared deadline across all connections — not per peer, or N
    peers could hold shutdown for N×drain_seconds) for in-flight work
    to finish (whatever is still outstanding at the deadline is
    reported in the outbound GOODBYE frame), tears each session down,
    and returns the total count of connections closed. Never raises:
    shutdown must proceed even if a transport is already dead.
    """
    count = 0
    deadline = time.monotonic() + drain_seconds

    # Outbound: wait for in-flight to finish, then send GOODBYE.
    for client in live_clients():
        while client.in_flight > 0 and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
        try:
            await client.close(in_flight=client.in_flight)
        except Exception:
            logger.debug(
                "peer GOODBYE on shutdown failed for %s",
                client.remote_orch_id,
                exc_info=True,
            )
        count += 1

    # Inbound: wait for in-flight INVOKEs to flush their RESULT, then
    # sentinel the outbound queue so the writer loop exits. The HTTP
    # request's StreamingResponse finishes when the body generator
    # returns — the FastAPI cleanup chain will close the client socket.
    for conn in live_inbound():
        while conn.in_flight > 0 and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
        try:
            await conn.aclose()
        except Exception:
            logger.debug(
                "peer inbound aclose on shutdown failed for %s",
                conn.remote_orch_id,
                exc_info=True,
            )
        count += 1

    return count
