"""Realtime pub/sub broker (``docs/FEATURE_GAP_VS_MULTICA.md`` §5.1).

Process-internal pub/sub backbone that fans events from producers
(BackendRunner, mutation routers) out to subscribed WebSocket
connections. The broker is intentionally minimal: an in-process
``asyncio.Queue`` per subscriber, topic-routed by string prefix match,
and a single ``get_broker()`` accessor so routers and the WebSocket
handler share the same instance without going through FastAPI state.

Wire protocol (mirrors ``docs/FEATURE_GAP_VS_MULTICA.md`` §5.4.1):

    {"topic": "<topic>", "payload": <obj>}

is broadcast verbatim to every subscriber whose topic set intersects
``<topic>``. The WebSocket handler is responsible for JSON-encoding and
framing; the broker stays protocol-agnostic so it can be reused by the
SSE endpoint and the daemon uplink.

Single-instance only — multica's ``server/internal/realtime`` adds a
Redis pub/sub relay for fan-out across server instances. Phase 1
peer federation (DESIGN AC7) realises that seam: the fan-out lives
behind the :class:`RealtimeBackend` protocol, :class:`LocalBackend`
reproduces the historical behavior exactly (AC13: ``publish``
behaviour unchanged), and :class:`RedisBackend` additionally relays
``peer.``-prefixed topics over Redis pub/sub so remote daemons'
events land here via ``peer/redis_relay.py`` (D22).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any, Protocol

from orchestratord.peer.agent_topics import is_peer_topic
from orchestratord.peer.redis_relay import peer_channel

if TYPE_CHECKING:
    from orchestratord.peer.redis_relay import PeerEventRelay

logger = logging.getLogger(__name__)


class RealtimeBackend(Protocol):
    """Fan-out strategy behind :class:`RealtimeBroker` (AC7).

    ``publish`` must return the number of local subscribers the frame
    was delivered to, matching the historical :meth:`RealtimeBroker.
    publish` contract.
    """

    async def publish(self, topic: str, payload: Any) -> int: ...


class LocalBackend:
    """Single-process fan-out — the default and historical behavior."""

    def __init__(self, broker: RealtimeBroker) -> None:
        self._broker = broker

    async def publish(self, topic: str, payload: Any) -> int:
        return await self._broker._fanout_local(topic, payload)


class RedisBackend:
    """Local fan-out plus Redis pub/sub relay for peer topics (AC7/D22).

    The local fan-out runs first and is identical to
    :class:`LocalBackend`'s (AC13: ``publish`` behaviour unchanged);
    the Redis hop is additive and only for ``peer.``-prefixed topics
    (R11), published on the D22 channel
    ``orch:peer:{orch_id}:topic:{topic}``.
    """

    def __init__(
        self,
        broker: RealtimeBroker,
        *,
        orch_id: str,
        redis_url: str = "redis://localhost:6379/0",
        redis_client: Any | None = None,
    ) -> None:
        self._broker = broker
        self._orch_id = orch_id
        if redis_client is not None:
            self._redis = redis_client
        else:
            import redis.asyncio as aioredis

            self._redis = aioredis.from_url(redis_url, decode_responses=True)
        self.relay: PeerEventRelay | None = None

    async def publish(self, topic: str, payload: Any) -> int:
        delivered = await self._broker._fanout_local(topic, payload)
        if is_peer_topic(topic):
            frame = json.dumps({"topic": topic, "payload": payload})
            await self._redis.publish(peer_channel(self._orch_id, topic), frame)
        return delivered

    async def start_relay(self) -> None:
        """Begin landing remote peers' Redis frames into the local broker."""
        if self.relay is None:
            from orchestratord.peer.redis_relay import PeerEventRelay

            self.relay = PeerEventRelay(
                self._broker,
                orch_id=self._orch_id,
                # Reuse this backend's client so tests can inject one fake.
                redis_client=self._redis,
            )
        await self.relay.start()

    async def aclose(self) -> None:
        if self.relay is not None:
            await self.relay.stop()
            self.relay = None
        await self._redis.aclose()


class RealtimeBroker:
    """Topic-based pub/sub broker.

    Subscribers receive an :class:`AsyncIterator` that yields
    ``{"topic": str, "payload": Any}`` frames. Producers call
    :meth:`publish` to fan out to all matching subscribers.

    The broker is process-local. Each subscriber owns an
    ``asyncio.Queue``; ``publish`` writes to every queue whose topic set
    matches. Slow consumers are evicted via :meth:`unsubscribe` so a
    stuck subscriber cannot block the daemon uplink.
    """

    def __init__(
        self,
        *,
        queue_size: int = 500,
        backend: RealtimeBackend | None = None,
    ) -> None:
        self._queue_size = queue_size
        # LocalBackend reproduces the historical single-process fan-out
        # byte-for-byte (AC13); RedisBackend adds the peer relay hop.
        self._backend = backend if backend is not None else LocalBackend(self)
        self._subscribers: dict[
            int, tuple[asyncio.Task[Any] | None, set[str], asyncio.Queue[dict[str, Any]]]
        ] = {}
        self._lock = asyncio.Lock()
        self._next_sub_id = 0

    async def subscribe(
        self, topics: set[str]
    ) -> tuple[int, AsyncIterator[dict[str, Any]]]:
        """Register a subscriber and return ``(sub_id, frame_iterator)``.

        The async iterator yields ``{"topic": ..., "payload": ...}``
        frames until the consumer's task is cancelled (or the
        iterator's ``__anext__`` raises), at which point the
        subscription is removed automatically via
        :meth:`unsubscribe`.

        ``sub_id`` is an auto-incrementing integer unique per broker
        instance — multiple ``subscribe()`` calls from the same task
        (e.g. tests, or one task opening several sockets) get distinct
        ids. The current task is kept alongside the subscription so
        :meth:`publish` can GC dead-task subscribers whose iterators
        never ran.
        """
        task = asyncio.current_task()
        async with self._lock:
            self._next_sub_id += 1
            sub_id = self._next_sub_id
            queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=self._queue_size)
            self._subscribers[sub_id] = (task, set(topics), queue)

        async def _iter() -> AsyncIterator[dict[str, Any]]:
            try:
                while True:
                    frame = await queue.get()
                    yield frame
            finally:
                await self.unsubscribe(sub_id)

        return sub_id, _iter()

    async def unsubscribe(self, sub_id: int) -> None:
        """Drop a subscriber by its task id (no-op if already removed)."""
        async with self._lock:
            self._subscribers.pop(sub_id, None)

    async def update_topics(self, sub_id: int, topics: set[str]) -> bool:
        """Replace a subscriber's topic set in place.

        Returns ``True`` if the subscriber was found and updated. Mutating
        in place avoids the churn of unsubscribe/resubscribe on every
        client ``subscribe``/``unsubscribe`` frame — the WebSocket handler
        keeps a stable ``sub_id`` for the life of the connection.
        """
        async with self._lock:
            entry = self._subscribers.get(sub_id)
            if entry is None:
                return False
            task, _old_topics, queue = entry
            self._subscribers[sub_id] = (task, set(topics), queue)
            return True

    async def publish(self, topic: str, payload: Any) -> int:
        """Fan out ``payload`` via the injected backend (AC7).

        Returns the number of local subscribers the frame was delivered
        to. With the default ``LocalBackend`` this is byte-for-byte the
        historical behavior (AC13: publish behaviour unchanged).
        """
        return await self._backend.publish(topic, payload)

    async def ingest_remote(self, topic: str, payload: Any) -> int:
        """Land an event that arrived from a remote peer (redis_relay).

        Deliberately separate from :meth:`publish`: remote frames enter
        the local fan-out directly and never re-enter the Redis relay,
        which is both the loop guard and what keeps AC13's "publish
        behaviour unchanged" literally true.
        """
        return await self._fanout_local(topic, payload)

    async def _fanout_local(self, topic: str, payload: Any) -> int:
        delivered = 0
        async with self._lock:
            snapshot = list(self._subscribers.items())
        for _sub_id, (task, topics, queue) in snapshot:
            if task.done():
                # Stale subscriber; drop it.
                async with self._lock:
                    self._subscribers.pop(_sub_id, None)
                continue
            if topic in topics:
                try:
                    queue.put_nowait({"topic": topic, "payload": payload})
                    delivered += 1
                except asyncio.QueueFull:
                    logger.warning(
                        "realtime broker dropping slow subscriber at topic=%s",
                        topic,
                    )
        return delivered


_broker: RealtimeBroker | None = None


def get_broker() -> RealtimeBroker:
    """Return the process-wide broker, creating it lazily.

    Laziness lets tests instantiate ``RealtimeBroker`` directly without
    going through module state; production code paths (the
    ``realtime`` router and mutation routers) call this accessor.

    When ``ORCHESTRATORD_REDIS_URL`` is configured the broker runs the
    :class:`RedisBackend` (AC7/D22): ``peer.``-topic publications are
    mirrored onto the daemon's D22 channel and a relay task lands
    remote daemons' frames into the local broker. Unset (the default,
    and every test process) keeps the pure in-process ``LocalBackend``
    — publish behaviour is byte-for-byte unchanged (AC13).
    """
    global _broker
    if _broker is None:
        broker = RealtimeBroker()  # LocalBackend until proven otherwise
        redis_url = os.environ.get("ORCHESTRATORD_REDIS_URL", "").strip()
        if redis_url:
            try:
                from orchestratord.peer.card import ensure_orch_id

                backend = RedisBackend(
                    broker, orch_id=ensure_orch_id(), redis_url=redis_url
                )
                # The broker defaults to LocalBackend(self); swap in the
                # RedisBackend now that both halves exist.
                broker._backend = backend
                _broker = broker
                _start_relay_soon(backend)
            except Exception:
                logger.warning(
                    "RedisBackend unavailable; staying on LocalBackend",
                    exc_info=True,
                )
        if _broker is None:
            _broker = broker
    return _broker


def _start_relay_soon(backend: RedisBackend) -> None:
    """Start the inbound relay once a loop is running (best-effort)."""
    import asyncio

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return

    async def _start() -> None:
        try:
            await backend.start_relay()
        except Exception:
            logger.warning(
                "peer redis relay failed to start", exc_info=True
            )

    loop.create_task(_start(), name="peer-relay-starter")


def reset_broker() -> None:
    """Drop the process-wide broker (test helper)."""
    global _broker
    _broker = None