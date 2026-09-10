"""Per-peer frame EVENT topic registry (PR-B9).

Under the batch-POST frame binding one HTTP request is one batch —
there is no long-lived response stream to push unsolicited EVENT
frames onto. SUBSCRIBE/UNSUBSCRIBE frames received in any batch
mutate the process-local topic set keyed by the peer's ``orch_id``
here, and the SSE endpoint (``GET /api/peer/peers/{orch_id}/events``)
consumes that set as its broker subscription.

Lifecycle: a peer's set starts empty; entries are added/removed by
SUBSCRIBE/UNSUBSCRIBE frames; ``GOODBYE``/``REVOKE`` from that peer
clear the whole set (the client re-subscribes after reconnecting);
:func:`reset_peer_topics` is the shutdown/test seam. Mirrors the
process-local design of :mod:`orchestratord.peer.connections`.
"""

from __future__ import annotations

_TOPIC_PREFIX = "peer."


_PEERS: dict[str, set[str]] = {}


def get_peer_topics(orch_id: str) -> set[str]:
    """Return the live topic set for *orch_id* (a copy — callers may mutate)."""
    return set(_PEERS.get(orch_id, set()))


def add_peer_topic(orch_id: str, topic: str) -> None:
    """Register one topic for *orch_id*; non-``peer.`` topics are ignored."""
    if not topic.startswith(_TOPIC_PREFIX):
        return
    _PEERS.setdefault(orch_id, set()).add(topic)


def remove_peer_topic(orch_id: str, topic: str) -> None:
    """Drop one topic for *orch_id* (no-op when absent)."""
    topics = _PEERS.get(orch_id)
    if topics is not None:
        topics.discard(topic)


def clear_peer_topics(orch_id: str) -> None:
    """Forget every topic registered for *orch_id* (GOODBYE/REVOKE)."""
    _PEERS.pop(orch_id, None)


def reset_peer_topics() -> None:
    """Drop every entry (daemon shutdown drain / test seam)."""
    _PEERS.clear()


__all__ = [
    "add_peer_topic",
    "clear_peer_topics",
    "get_peer_topics",
    "remove_peer_topic",
    "reset_peer_topics",
]
