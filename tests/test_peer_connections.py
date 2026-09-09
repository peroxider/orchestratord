"""PR6 wiring tests: D24 drain, §6.1d wake, §6.2 forward hook, AC7 backend.

Covers the daemon-side glue that earlier PR unit tests could not reach:

* :func:`orchestratord.peer.connections.shutdown_peer_connections` — D24
  drain-then-GOODBYE semantics (deadline, dead transports).
* :meth:`orchestratord.chat_dispatcher.ChatDispatcher.wake` — the §6.1d
  interrupt that lets a peer auto-scheduled turn skip the poll interval.
* :func:`orchestratord.api.routers.sessions._forward_to_peer` — the
  §6.2 Phase-B bridge hook (capability-gated, never raises).
* :func:`orchestratord.api.realtime.get_broker` — AC7 backend selection
  (LocalBackend by default, RedisBackend under ``ORCHESTRATORD_REDIS_URL``).
"""

from __future__ import annotations

import asyncio
import time
import uuid
from contextlib import suppress
from typing import Any

import pytest

from orchestratord.api.realtime import (
    LocalBackend,
    RedisBackend,
    get_broker,
    reset_broker,
)
from orchestratord.api.routers import sessions as sessions_router
from orchestratord.chat_dispatcher import ChatDispatcher
from orchestratord.peer import connections
from orchestratord.peer.connections import (
    live_clients,
    register_client,
    shutdown_peer_connections,
    unregister_client,
)
from orchestratord.spi.approval import ApprovalDecision


class _FakeClient:
    """Minimal PeerClient stand-in for registry/drain/forward tests."""

    def __init__(
        self,
        *,
        in_flight: int = 0,
        close_error: Exception | None = None,
        capabilities: tuple[str, ...] = (),
    ) -> None:
        self._in_flight = in_flight
        self.close_error = close_error
        self.close_calls: list[int] = []
        self.remote_orch_id = "fake-orch"
        self.remote_capabilities = list(capabilities)
        self.invocations: list[tuple[str, dict[str, Any] | None, str | None]] = []

    @property
    def in_flight(self) -> int:
        return self._in_flight

    async def close(self, *, in_flight: int = 0) -> None:
        # Faithful to PeerClient.close: unregister first, even on failure.
        unregister_client(self)
        if self.close_error is not None:
            raise self.close_error
        self.close_calls.append(in_flight)

    async def invoke(
        self,
        method: str,
        body: dict[str, Any] | None = None,
        *,
        request_id: str | None = None,
        **_kwargs: Any,
    ) -> None:
        self.invocations.append((method, body, request_id))


@pytest.fixture(autouse=True)
def empty_registry():
    connections._live.clear()
    yield
    connections._live.clear()


# ---------------------------------------------------------------------------
# D24: connection registry + shutdown drain
# ---------------------------------------------------------------------------


async def test_register_and_unregister_roundtrip() -> None:
    client = _FakeClient()
    register_client(client)
    assert live_clients() == [client]
    unregister_client(client)
    assert live_clients() == []


async def test_shutdown_drains_then_says_goodbye() -> None:
    client = _FakeClient(in_flight=1)
    register_client(client)

    async def finish_soon() -> None:
        await asyncio.sleep(0.05)
        client._in_flight = 0

    asyncio.create_task(finish_soon())

    count = await shutdown_peer_connections(drain_seconds=2.0)
    assert count == 1
    assert client.close_calls == [0]  # drained before GOODBYE
    assert live_clients() == []


async def test_shutdown_deadline_reports_remaining_in_flight() -> None:
    client = _FakeClient(in_flight=1)  # never drains
    register_client(client)

    count = await shutdown_peer_connections(drain_seconds=0.1)
    assert count == 1
    assert client.close_calls == [1]  # deadline hit; count reported verbatim


async def test_shutdown_survives_dead_transport() -> None:
    dead = _FakeClient(close_error=ConnectionError("transport already dead"))
    healthy = _FakeClient()
    register_client(dead)
    register_client(healthy)

    count = await shutdown_peer_connections(drain_seconds=0.1)
    assert count == 2  # the dead peer still gets its goodbye attempt
    assert healthy.close_calls == [0]
    assert live_clients() == []


async def test_shutdown_drain_deadline_is_shared_across_peers() -> None:
    """D24's drain budget is total, not per peer: four peers that never
    drain must all hit the one shared deadline (~drain_seconds), not
    serially consume 4×drain_seconds."""
    stuck = [_FakeClient(in_flight=1) for _ in range(4)]
    for client in stuck:
        register_client(client)

    started = time.monotonic()
    count = await shutdown_peer_connections(drain_seconds=0.2)
    elapsed = time.monotonic() - started

    assert count == 4
    assert all(c.close_calls == [1] for c in stuck)
    assert elapsed < 0.5  # shared deadline; per-peer would exceed 0.8


# ---------------------------------------------------------------------------
# §6.1d: wake interrupts the dispatcher's idle poll
# ---------------------------------------------------------------------------


async def _wait_until(predicate, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("condition not met before timeout")
        await asyncio.sleep(0.01)


async def test_wake_interrupts_idle_poll() -> None:
    claims = 0

    async def fake_claim() -> None:
        nonlocal claims
        claims += 1

    dispatcher = ChatDispatcher(None, None, interval=30.0)
    dispatcher.claim_next = fake_claim  # shadow the real DB claim
    task = asyncio.create_task(dispatcher.run_forever())
    try:
        await _wait_until(lambda: claims >= 1)
        assert not task.done()

        dispatcher.wake()
        # Without wake() the next claim would only happen after 30s.
        await _wait_until(lambda: claims >= 2, timeout=1.0)
    finally:
        dispatcher.stop()
        with suppress(asyncio.CancelledError, asyncio.TimeoutError):
            await asyncio.wait_for(task, timeout=1.0)


# ---------------------------------------------------------------------------
# §6.2: capability-gated peer forward hook
# ---------------------------------------------------------------------------


async def test_forward_to_peer_gated_on_capability() -> None:
    capable = _FakeClient(capabilities=("peer.approval.forward",))
    incapable = _FakeClient()
    register_client(incapable)
    register_client(capable)

    session_id = uuid.uuid4()
    await sessions_router._forward_to_peer(
        session_id, "peer.approval.forward", {"request_id": "r-1"}
    )

    assert incapable.invocations == []
    assert capable.invocations == [
        (
            "peer.approval.forward",
            {"request_id": "r-1", "session_id": str(session_id)},
            f"fwd-{session_id}-peer.approval.forward",
        )
    ]


async def test_forward_to_peer_swallows_transport_failures() -> None:
    class _BoomClient(_FakeClient):
        async def invoke(self, method, body=None, *, request_id=None, **_kw):
            raise ConnectionError("peer transport dead")

    register_client(_BoomClient(capabilities=("peer.interrupt.forward",)))

    # Must not raise — the DB decision record is the source of truth.
    await sessions_router._forward_to_peer(
        uuid.uuid4(), "peer.interrupt.forward", {}
    )


async def test_forward_approval_no_live_session_hits_peer_hook() -> None:
    peer_client = _FakeClient(capabilities=("peer.approval.forward",))
    register_client(peer_client)

    session_id = uuid.uuid4()
    # backend_runner=None → _lookup_live returns None → peer hook fires.
    await sessions_router._forward_approval(
        session_id, None, "req-9", ApprovalDecision.ALLOW
    )

    assert len(peer_client.invocations) == 1
    method, body, _rid = peer_client.invocations[0]
    assert method == "peer.approval.forward"
    assert body == {
        "request_id": "req-9",
        "decision": "allow",
        "session_id": str(session_id),
    }


# ---------------------------------------------------------------------------
# AC7: broker backend selection
# ---------------------------------------------------------------------------


@pytest.fixture()
def clean_broker(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "orchestratord.peer.card.ORCH_ID_PATH", tmp_path / "data" / "orch_id"
    )
    monkeypatch.delenv("ORCHESTRATORD_REDIS_URL", raising=False)
    monkeypatch.delenv("ORCHESTRATORD_INSTANCE_NAME", raising=False)
    reset_broker()
    yield
    reset_broker()


async def test_broker_defaults_to_local_backend(clean_broker) -> None:
    broker = get_broker()
    assert isinstance(broker._backend, LocalBackend)


async def test_broker_selects_redis_backend_when_env_set(
    clean_broker, monkeypatch
) -> None:
    monkeypatch.setenv("ORCHESTRATORD_REDIS_URL", "redis://localhost:6379/0")
    broker = get_broker()
    backend = broker._backend
    assert isinstance(backend, RedisBackend)

    # Let the relay-starter task finish so nothing dangles past the test.
    await asyncio.sleep(0)
    for task in asyncio.all_tasks():
        if task.get_name() == "peer-relay-starter":
            with suppress(Exception):
                await task
    await backend.aclose()
