"""Redis pub/sub relay for peer federation (DESIGN §7 R8, D22, AC7).

A daemon's ``RedisBackend`` publishes ``peer.``-prefixed broker topics
to channels named

    orch:peer:{orch_id}:topic:{topic_name}          (D22)

where ``{orch_id}`` identifies the *origin* daemon. Every peer's relay
PSUBSCRIBEs the wildcard pattern, decodes each frame, and lands it in
its *local* broker via ``RealtimeBroker.ingest_remote`` — so existing
``/ws`` subscribers receive remote agent events with zero route
changes (AC7). The ``orch:peer:{orch_id}`` namespace keeps multiple
daemons sharing one operator-managed Redis isolated (D22, R11) and
gives the relay its echo guard: frames whose origin equals the local
orch_id are dropped, so a relayed event never loops.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

# D22: orch:peer:{orch_id}:topic:{topic_name}
REDIS_CHANNEL_PREFIX = "orch:peer"
PEER_CHANNEL_PATTERN = f"{REDIS_CHANNEL_PREFIX}:*:topic:*"


def peer_channel(orch_id: str, topic: str) -> str:
    """D22 channel on which *orch_id* publishes *topic*."""
    return f"{REDIS_CHANNEL_PREFIX}:{orch_id}:topic:{topic}"


def parse_peer_channel(channel: str) -> tuple[str, str] | None:
    """Split a D22 channel into ``(origin_orch_id, topic)``.

    Returns ``None`` for any channel outside the ``orch:peer`` namespace
    so a shared Redis carrying unrelated pub/sub traffic is ignored.
    The topic may itself contain colons; only the four fixed separators
    are consumed.
    """
    parts = channel.split(":", 4)
    if (
        len(parts) != 5
        or parts[0] != "orch"
        or parts[1] != "peer"
        or parts[3] != "topic"
        or not parts[2]
        or not parts[4]
    ):
        return None
    return parts[2], parts[4]


class PeerEventRelay:
    """Bridge remote peer events into the local broker (R8, AC7).

    One asyncio task PSUBSCRIBEs :data:`PEER_CHANNEL_PATTERN`, decodes
    each frame and feeds :meth:`RealtimeBroker.ingest_remote`. Frames
    from the local daemon itself (echo), undecodable bodies, and
    channels outside the D22 namespace are dropped.

    ``redis_client`` is a test seam; production passes only a
    ``redis_url`` and the client is built with ``decode_responses=True``
    (frames are JSON text).
    """

    def __init__(
        self,
        broker: Any,
        *,
        orch_id: str,
        redis_url: str = "redis://localhost:6379/0",
        pattern: str = PEER_CHANNEL_PATTERN,
        redis_client: Any | None = None,
    ) -> None:
        self._broker = broker
        self._orch_id = orch_id
        self._pattern = pattern
        if redis_client is not None:
            self._redis = redis_client
        else:
            import redis.asyncio as aioredis

            self._redis = aioredis.from_url(redis_url, decode_responses=True)
        self._pubsub: Any | None = None
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if self._task is not None:
            return
        self._pubsub = self._redis.pubsub()
        await self._pubsub.psubscribe(self._pattern)
        self._task = asyncio.create_task(self._listen(), name="peer-redis-relay")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        if self._pubsub is not None:
            await self._pubsub.aclose()
            self._pubsub = None
        await self._redis.aclose()

    async def _listen(self) -> None:
        assert self._pubsub is not None
        # listen() ends only on cancel or connection loss; either way the
        # task just exits and stop()/restart owns the lifecycle.
        async for msg in self._pubsub.listen():
            if msg.get("type") != "pmessage":
                continue
            await self._handle_channel(msg.get("channel"), msg.get("data"))

    async def _handle_channel(self, channel: Any, data: Any) -> None:
        if not isinstance(channel, str):
            return
        parsed = parse_peer_channel(channel)
        if parsed is None:
            return
        origin, topic = parsed
        if origin == self._orch_id:
            return  # echo guard: never re-ingest our own publications
        if not isinstance(data, str):
            return
        try:
            frame = json.loads(data)
        except ValueError:
            logger.warning("peer relay dropping undecodable frame on %s", channel)
            return
        if not isinstance(frame, dict):
            return
        await self._broker.ingest_remote(topic, frame.get("payload"))
