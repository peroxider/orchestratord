"""RealtimeBroker unit tests (``docs/FEATURE_GAP_VS_MULTICA.md`` §5.1, §5.7).

The broker is the in-process pub/sub backbone for the Phase A realtime
fan-out. These tests pin its contract independently of the FastAPI
WebSocket layer so the broker can be reused by the SSE endpoint and
daemon uplink without re-testing the protocol plumbing.

Acceptance covered (per §5.7):
- "3 subscribers, 1 publish → all 3 see the frame"
- Topic set is mutated in place via ``update_topics``
- Slow consumer is dropped with a warning (mirrors multica ``SlowConsumer``)
- ``unsubscribe`` is idempotent
"""

from __future__ import annotations

import asyncio

import pytest

from orchestratord.api.realtime import RealtimeBroker


@pytest.mark.asyncio
async def test_publish_fans_out_to_every_matching_subscriber() -> None:
    """§5.7: 3 subscribers on the same topic receive the same frame."""
    broker = RealtimeBroker()
    _sub_a, iter_a = await broker.subscribe({"session.42"})
    _sub_b, iter_b = await broker.subscribe({"session.42"})
    _sub_c, iter_c = await broker.subscribe({"session.42"})

    delivered = await broker.publish("session.42", {"delta": "hello"})

    assert delivered == 3
    for iterator in (iter_a, iter_b, iter_c):
        frame = await asyncio.wait_for(iterator.__anext__(), timeout=1.0)
        assert frame == {"topic": "session.42", "payload": {"delta": "hello"}}


@pytest.mark.asyncio
async def test_publish_skips_subscribers_whose_topic_set_does_not_match() -> None:
    broker = RealtimeBroker()
    _match, match_iter = await broker.subscribe({"session.42"})
    _miss, miss_iter = await broker.subscribe({"session.99"})

    delivered = await broker.publish("session.42", {"delta": "x"})

    assert delivered == 1
    frame = await asyncio.wait_for(match_iter.__anext__(), timeout=1.0)
    assert frame["topic"] == "session.42"


@pytest.mark.asyncio
async def test_update_topics_replaces_subscription_in_place() -> None:
    """The WS handler calls update_topics on every subscribe/unsubscribe frame."""
    broker = RealtimeBroker()
    sub_id, frame_iter = await broker.subscribe({"session.42"})

    assert await broker.update_topics(sub_id, {"session.99"}) is True
    assert await broker.publish("session.42", {"x": 1}) == 0
    delivered = await broker.publish("session.99", {"y": 2})
    assert delivered == 1
    frame = await asyncio.wait_for(frame_iter.__anext__(), timeout=1.0)
    assert frame == {"topic": "session.99", "payload": {"y": 2}}


@pytest.mark.asyncio
async def test_update_topics_returns_false_for_unknown_subscriber() -> None:
    broker = RealtimeBroker()
    assert await broker.update_topics(99999, {"session.1"}) is False


@pytest.mark.asyncio
async def test_unsubscribe_is_idempotent() -> None:
    broker = RealtimeBroker()
    sub_id, _iter = await broker.subscribe({"session.1"})
    await broker.unsubscribe(sub_id)
    # Second call must not raise (dict.pop default).
    await broker.unsubscribe(sub_id)
    # Publishing afterwards delivers to zero subscribers.
    assert await broker.publish("session.1", {"x": 1}) == 0


@pytest.mark.asyncio
async def test_slow_consumer_is_dropped_with_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A subscriber whose queue is full is dropped on the next publish,
    matching multica ``SlowConsumer`` semantics (warning, not back-pressure).
    """
    broker = RealtimeBroker(queue_size=2)
    sub_id, _iter = await broker.subscribe({"session.1"})

    # Fill the queue past its maxsize so the next publish hits QueueFull.
    await broker.publish("session.1", {"i": 0})
    await broker.publish("session.1", {"i": 1})
    with caplog.at_level("WARNING", logger="orchestratord.api.realtime"):
        delivered = await broker.publish("session.1", {"i": 2})
    assert delivered == 0
    assert any(
        "slow subscriber" in record.message for record in caplog.records
    )


@pytest.mark.asyncio
async def test_iterator_unsubscribes_on_task_cancellation() -> None:
    """The broker's iterator cleanup must run when the consumer's task is
    cancelled (e.g. WebSocket handler's broadcast loop)."""
    broker = RealtimeBroker()
    sub_id, frame_iter = await broker.subscribe({"session.1"})

    task = asyncio.current_task()
    assert task is not None
    # sub_id is an auto-incrementing integer unique per broker instance,
    # not a function of the subscribing task's id (multiple subscribes
    # from the same task must get distinct ids).
    assert isinstance(sub_id, int) and sub_id > 0

    publish_task = asyncio.create_task(broker.publish("session.1", {"x": 1}))
    await asyncio.sleep(0)  # let publish() put the frame
    frame = await asyncio.wait_for(frame_iter.__anext__(), timeout=1.0)
    assert frame == {"topic": "session.1", "payload": {"x": 1}}
    assert await publish_task == 1

    # The iterator's ``finally`` fires when the task is cancelled while
    # suspended on ``queue.get()``; drive the cleanup by raising into it.
    task.cancel()
    try:
        await frame_iter.__anext__()
    except (asyncio.CancelledError, StopAsyncIteration):
        pass

    # Give the broker a moment to apply the iterator's finally cleanup.
    for _ in range(20):
        await asyncio.sleep(0)
        if await broker.publish("session.1", {"x": 2}) == 0:
            break
    assert await broker.publish("session.1", {"x": 2}) == 0
