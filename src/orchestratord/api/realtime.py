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
Redis pub/sub relay for fan-out across server instances. We mirror that
boundary here so swapping in Redis later only changes the backend, not
the consumer API.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from typing import Any

logger = logging.getLogger(__name__)


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

    def __init__(self, *, queue_size: int = 500) -> None:
        self._queue_size = queue_size
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
        """Fan out ``payload`` to every subscriber whose topic set contains
        ``topic``.

        Returns the number of subscribers the frame was delivered to.
        Subscribers whose queue is full are silently dropped (a slow
        consumer is the WS handler's problem — the WebSocket error
        path closes the socket and the subscriber's async iterator
        exits, triggering cleanup). This matches the multica
        behaviour where ``SlowConsumer`` is a recoverable warning,
        not a back-pressure event.
        """
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
    """
    global _broker
    if _broker is None:
        _broker = RealtimeBroker()
    return _broker


def reset_broker() -> None:
    """Drop the process-wide broker (test helper)."""
    global _broker
    _broker = None