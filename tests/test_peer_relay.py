"""PR3 unit tests: RealtimeBackend abstraction, Redis relay, D22 channels.

Covers the AC7 seam without a live Redis: an in-memory bus reproduces
the redis-py asyncio surface (``publish``/``pubsub().psubscribe()/
listen()``, ``set(..., nx=True, ex=...)``) so the fan-out delegation,
the ``orch:peer:{orch_id}:topic:{topic}`` channel naming (D22), the
relay's echo guard, and the SETNX nonce backend are all exercised
end-to-end across two simulated daemons.
"""

from __future__ import annotations

import asyncio
import fnmatch
import json

import pytest

from orchestratord.api.realtime import (
    LocalBackend,
    RealtimeBroker,
    RedisBackend,
    get_broker,
)
from orchestratord.peer.agent_topics import agent_topic, is_peer_topic
from orchestratord.peer.hmac_sig import PeerAuthError, sign, verify
from orchestratord.peer.nonce_store import RedisNonceStore
from orchestratord.peer.protocol import PeerFrame, PeerFrameType
from orchestratord.peer.redis_relay import (
    PEER_CHANNEL_PATTERN,
    PeerEventRelay,
    parse_peer_channel,
    peer_channel,
)

# -- fakes reproducing the redis-py asyncio surface --

class _FakePubSub:
    def __init__(self, bus: _FakeBus) -> None:
        self._bus = bus
        self._queue: asyncio.Queue[dict] = asyncio.Queue()
        self._pattern: str | None = None

    async def psubscribe(self, pattern: str) -> None:
        self._pattern = pattern
        self._bus._register(self, pattern)

    def _feed(self, msg: dict) -> None:
        self._queue.put_nowait(msg)

    async def listen(self):
        while True:
            yield await self._queue.get()

    async def aclose(self) -> None:
        self._bus._unregister(self)


class _FakeBus:
    def __init__(self) -> None:
        self._subs: list[tuple[_FakePubSub, str]] = []
        self.published: list[tuple[str, str]] = []
        self._set_keys: set[str] = set()

    async def publish(self, channel: str, body: str) -> int:
        self.published.append((channel, body))
        delivered = 0
        for sub, pattern in list(self._subs):
            if fnmatch.fnmatchcase(channel, pattern):
                sub._feed(
                    {"type": "pmessage", "channel": channel,
                     "pattern": pattern, "data": body}
                )
                delivered += 1
        return delivered

    def pubsub(self) -> _FakePubSub:
        return _FakePubSub(self)

    async def set(self, key: str, value: object, *, nx: bool = False,
                  ex: int | None = None) -> bool | None:
        if nx and key in self._set_keys:
            return None
        self._set_keys.add(key)
        return True

    def _register(self, sub: _FakePubSub, pattern: str) -> None:
        self._subs.append((sub, pattern))

    def _unregister(self, sub: _FakePubSub) -> None:
        self._subs = [(s, p) for s, p in self._subs if s is not sub]


class _FakeClient:
    """One daemon's handle on the shared bus."""

    def __init__(self, bus: _FakeBus) -> None:
        self.bus = bus

    async def publish(self, channel: str, body: str) -> int:
        return await self.bus.publish(channel, body)

    def pubsub(self) -> _FakePubSub:
        return self.bus.pubsub()

    async def set(self, key: str, value: object, *, nx: bool = False,
                  ex: int | None = None) -> bool | None:
        return await self.bus.set(key, value, nx=nx, ex=ex)

    async def aclose(self) -> None:
        pass


class _RecordingBroker:
    """Captures ingest_remote calls for relay unit tests."""

    def __init__(self) -> None:
        self.ingested: list[tuple[str, object]] = []

    async def ingest_remote(self, topic: str, payload: object) -> int:
        self.ingested.append((topic, payload))
        return 1


# -- agent_topics (R11) --

def test_agent_topic_format() -> None:
    assert agent_topic("a7") == "peer.agent.a7.events"


def test_is_peer_topic_prefix_rule() -> None:
    assert is_peer_topic("peer.agent.a.events")
    assert not is_peer_topic("sessions.1.events")
    assert not is_peer_topic("peerless")  # prefix, not substring


# -- D22 channel naming --

def test_peer_channel_and_parse_round_trip() -> None:
    channel = peer_channel("orch-A1-2026-09-08-9f3c1a", agent_topic("a7"))
    assert channel == (
        "orch:peer:orch-A1-2026-09-08-9f3c1a:topic:peer.agent.a7.events"
    )
    assert parse_peer_channel(channel) == (
        "orch-A1-2026-09-08-9f3c1a", "peer.agent.a7.events"
    )


def test_parse_preserves_colons_inside_topic() -> None:
    channel = "orch:peer:orch-B1:topic:some:odd:topic"
    assert parse_peer_channel(channel) == ("orch-B1", "some:odd:topic")


@pytest.mark.parametrize(
    "channel",
    [
        "other:peer:orch-B1:topic:t",      # wrong namespace
        "orch:other:orch-B1:topic:t",      # wrong kind
        "orch:peer:orch-B1:chan:t",        # wrong segment
        "orch:peer::topic:t",              # empty orch_id
        "orch:peer:orch-B1:topic:",        # empty topic
        "orch:peer:orch-B1:topic",         # missing topic segment
    ],
)
def test_parse_rejects_foreign_channels(channel: str) -> None:
    assert parse_peer_channel(channel) is None


# -- backend delegation (AC13: LocalBackend behavior unchanged) --

def test_get_broker_defaults_to_local_backend() -> None:
    assert isinstance(get_broker()._backend, LocalBackend)


async def test_local_backend_publish_delivers_to_subscribers() -> None:
    broker = RealtimeBroker()
    sub_id, frames = await broker.subscribe({"t1"})
    delivered = await broker.publish("t1", {"n": 1})
    assert delivered == 1
    assert await asyncio.wait_for(anext(frames), timeout=2) == {
        "topic": "t1", "payload": {"n": 1}
    }
    await broker.unsubscribe(sub_id)


class _ExplodingBackend:
    async def publish(self, topic: str, payload: object) -> int:
        raise AssertionError("ingest_remote must not go through publish")


async def test_ingest_remote_bypasses_backend_publish() -> None:
    broker = RealtimeBroker(backend=_ExplodingBackend())
    sub_id, frames = await broker.subscribe({"peer.agent.x.events"})
    delivered = await broker.ingest_remote("peer.agent.x.events", {"n": 1})
    assert delivered == 1
    assert (await asyncio.wait_for(anext(frames), timeout=2))["payload"] == {"n": 1}
    await broker.unsubscribe(sub_id)


# -- RedisBackend publish path (D22 / R11) --

def _broker_with_redis_backend(orch_id: str, client: _FakeClient) -> RealtimeBroker:
    """A broker whose fan-out is a RedisBackend bound to itself.

    RedisBackend needs the broker reference (local fan-out) and the
    broker needs the backend — bound after construction.
    """
    broker = RealtimeBroker()
    broker._backend = RedisBackend(broker, orch_id=orch_id, redis_client=client)
    return broker


async def test_redis_backend_relays_peer_topic_to_d22_channel() -> None:
    bus = _FakeBus()
    broker = _broker_with_redis_backend("orch-A1", _FakeClient(bus))
    sub_id, frames = await broker.subscribe({agent_topic("a7")})
    delivered = await broker.publish(agent_topic("a7"), {"seq": 1})
    assert delivered == 1  # local fan-out still counted (AC13)
    assert (await asyncio.wait_for(anext(frames), timeout=2))["payload"] == {"seq": 1}
    assert len(bus.published) == 1
    channel, body = bus.published[0]
    assert channel == "orch:peer:orch-A1:topic:peer.agent.a7.events"
    assert json.loads(body) == {"topic": agent_topic("a7"), "payload": {"seq": 1}}
    await broker.unsubscribe(sub_id)


async def test_redis_backend_keeps_internal_topics_off_redis() -> None:
    bus = _FakeBus()
    broker = _broker_with_redis_backend("orch-A1", _FakeClient(bus))
    sub_id, frames = await broker.subscribe({"sessions.1.events"})
    assert await broker.publish("sessions.1.events", {"x": 1}) == 1
    assert (await asyncio.wait_for(anext(frames), timeout=2))["payload"] == {"x": 1}
    assert bus.published == []  # R11: only peer.* leaves the process
    await broker.unsubscribe(sub_id)


# -- PeerEventRelay --

async def test_relay_lands_remote_frame_in_local_broker() -> None:
    rec = _RecordingBroker()
    relay = PeerEventRelay(
        rec, orch_id="orch-A1",
        redis_client=_FakeClient(_FakeBus()),
    )
    await relay._handle_channel(
        peer_channel("orch-B1", agent_topic("a7")),
        json.dumps({"topic": agent_topic("a7"), "payload": {"seq": 3}}),
    )
    assert rec.ingested == [(agent_topic("a7"), {"seq": 3})]


async def test_relay_drops_own_echo() -> None:
    rec = _RecordingBroker()
    relay = PeerEventRelay(
        rec, orch_id="orch-B1",
        redis_client=_FakeClient(_FakeBus()),
    )
    await relay._handle_channel(
        peer_channel("orch-B1", agent_topic("a7")),
        json.dumps({"topic": agent_topic("a7"), "payload": {}}),
    )
    assert rec.ingested == []  # loop guard


async def test_relay_drops_undecodable_and_foreign_frames() -> None:
    rec = _RecordingBroker()
    relay = PeerEventRelay(
        rec, orch_id="orch-A1",
        redis_client=_FakeClient(_FakeBus()),
    )
    await relay._handle_channel(peer_channel("orch-B1", "t"), "not-json{")
    await relay._handle_channel(peer_channel("orch-B1", "t"), "[1, 2]")
    await relay._handle_channel("unrelated:channel", '{"topic": "t"}')
    await relay._handle_channel(None, '{"topic": "t"}')
    assert rec.ingested == []


async def test_two_daemon_flow_over_shared_bus() -> None:
    # G3/AC7 end-to-end over the fake bus: B1 publishes an agent event,
    # A1's /ws-style subscriber receives it through the relay.
    bus = _FakeBus()
    broker_b = _broker_with_redis_backend("orch-B1", _FakeClient(bus))
    backend_b = broker_b._backend
    broker_a = _broker_with_redis_backend("orch-A1", _FakeClient(bus))
    backend_a = broker_a._backend
    sub_id, frames = await broker_a.subscribe({agent_topic("a7")})
    await backend_a.start_relay()
    try:
        await broker_b.publish(agent_topic("a7"), {"seq": 1})
        frame = await asyncio.wait_for(anext(frames), timeout=2)
        assert frame == {"topic": agent_topic("a7"), "payload": {"seq": 1}}
        # B1's own subscribers see their publish exactly once — the
        # relay must not echo it back (origin == self is dropped).
        sub_b, frames_b = await broker_b.subscribe({agent_topic("a7")})
        await broker_b.publish(agent_topic("a7"), {"seq": 2})
        assert (await asyncio.wait_for(anext(frames_b), timeout=2))["payload"] == {"seq": 2}
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(anext(frames_b), timeout=0.05)
        await broker_b.unsubscribe(sub_b)
    finally:
        await backend_a.aclose()  # stops the relay task
        await backend_b.aclose()
        await broker_a.unsubscribe(sub_id)


async def test_relay_start_is_idempotent() -> None:
    bus = _FakeBus()
    relay = PeerEventRelay(
        _RecordingBroker(), orch_id="orch-A1", redis_client=_FakeClient(bus)
    )
    await relay.start()
    first_task = relay._task
    await relay.start()
    assert relay._task is first_task
    await relay.stop()
    assert relay._task is None


# -- RedisNonceStore (R2 SETNX) --

async def test_redis_nonce_store_first_sight_wins() -> None:
    store = RedisNonceStore(redis_client=_FakeClient(_FakeBus()))
    assert await store.check_and_store("orch-A1", "n1") is True
    assert await store.check_and_store("orch-A1", "n1") is False
    assert await store.check_and_store("orch-B1", "n1") is True
    assert await store.prune() == 0


async def test_verify_accepts_redis_nonce_store(tmp_path) -> None:
    store = RedisNonceStore(redis_client=_FakeClient(_FakeBus()))
    frame = sign(
        PeerFrame(
            type=PeerFrameType.INVOKE, orch_id="orch-A1",
            request_id="r", method="GET /x", body={"a": 1},
        ),
        "token",
    )
    await verify(frame, "token", store)  # must not raise
    with pytest.raises(PeerAuthError, match="nonce replay"):
        await verify(frame, "token", store)


def test_d22_pattern_matches_d22_channels() -> None:
    assert fnmatch.fnmatchcase(
        peer_channel("orch-A1", agent_topic("a7")), PEER_CHANNEL_PATTERN
    )
    assert not fnmatch.fnmatchcase("orch:other:topic:x", PEER_CHANNEL_PATTERN)
