"""Live outbound peer-connection registry (DESIGN §6.2; D24).

Phase 1 creates ``PeerClient`` connections on demand; this module is the
daemon-side handle on whichever ones are currently open so two behaviors
can find them:

* **D24 shutdown drain** — :func:`shutdown_peer_connections` waits up to
  five seconds for in-flight requests to drain, then sends each peer a
  GOODBYE frame carrying the final in-flight count. ``cli/serve.py``
  invokes it from the uvicorn shutdown path (SIGTERM/SIGINT both land
  there).
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

logger = logging.getLogger(__name__)

_DEFAULT_DRAIN_SECONDS = 5.0

_live: set[PeerClient] = set()


def register_client(client: PeerClient) -> None:
    """Track *client* after a successful handshake (PeerClient.open)."""
    _live.add(client)


def unregister_client(client: PeerClient) -> None:
    """Forget *client* once its session is torn down (PeerClient.close)."""
    _live.discard(client)


def live_clients() -> list[PeerClient]:
    """Snapshot of the currently open peer connections."""
    return list(_live)


async def shutdown_peer_connections(
    drain_seconds: float = _DEFAULT_DRAIN_SECONDS,
) -> int:
    """D24: drain in-flight work, then GOODBYE every connected peer.

    Waits up to *drain_seconds* **in total** (one shared deadline across
    all clients — not per peer, or N peers could hold shutdown for
    N×drain_seconds) for in-flight requests to finish (whatever is
    still outstanding at the deadline is reported in the GOODBYE frame),
    tears the session down, and returns how many peers were said
    goodbye to. Never raises: shutdown must proceed even if a transport
    is already dead.
    """
    count = 0
    deadline = time.monotonic() + drain_seconds
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
    return count
