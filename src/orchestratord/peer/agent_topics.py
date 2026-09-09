"""Per-agent broker topic naming for peer federation (DESIGN §7 R11, D22).

Agent events that may be relayed to peers live under the ``peer.``
topic prefix so they can never collide with the daemon's internal
broker topics (R11). The Redis channel adds a second
``orch:peer:{orch_id}`` namespace (see ``peer/redis_relay.py``, D22) —
double isolation between internal traffic and cross-daemon traffic.

G3: a local ``/ws`` subscriber on ``peer.agent.{agent_id}.events``
receives that agent's events even when they originate on a remote
daemon, once the relay is running.
"""

from __future__ import annotations

PEER_TOPIC_PREFIX = "peer."


def agent_topic(agent_id: str) -> str:
    """Broker topic carrying *agent_id*'s event stream across peers."""
    return f"{PEER_TOPIC_PREFIX}agent.{agent_id}.events"


def is_peer_topic(topic: str) -> bool:
    """True when *topic* is relayable to peers (R11 prefix rule)."""
    return topic.startswith(PEER_TOPIC_PREFIX)
