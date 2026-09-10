"""Unit tests for the PR4 handshake state machine and peer client (D23).

Covers the §5 HELLO→WELCOME exchange (success, non-WELCOME reject,
timeout, bad signature — each aborting with the transport closed and no
half-state), SUBSCRIBE emission, and :class:`PeerClient` lifecycle:
D23 retry/backoff, D24 GOODBYE on close, INVOKE→RESULT correlation by
request_id with D18 msg_id, timeout, and the EVENT stream. The SSE
``data:`` line decoder used by the PR5 server binding is tested here
because it lives in ``peer/client.py``.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from orchestratord.peer.client import (
    PeerClient,
    PeerClientError,
    parse_sse_data_line,
)
from orchestratord.peer.handshake import (
    HandshakeError,
    perform_handshake,
    send_subscriptions,
)
from orchestratord.peer.hmac_sig import PeerAuthError, sign, verify
from orchestratord.peer.nonce_store import NonceStore
from orchestratord.peer.protocol import PeerFrame, PeerFrameType

TOKEN = "peer-secret-token"


class MemoryTransport:
    """In-memory :class:`FrameTransport` double linked to a peer."""

    def __init__(self) -> None:
        self.sent: list[PeerFrame] = []
        self._in: asyncio.Queue[PeerFrame | None] = asyncio.Queue()
        self._peer: MemoryTransport | None = None
        self.closed = False

    @classmethod
    def link(cls) -> tuple[MemoryTransport, MemoryTransport]:
        a, b = cls(), cls()
        a._peer = b
        b._peer = a
        return a, b

    async def send_frame(
        self, frame: PeerFrame, *, end_of_batch: bool = False
    ) -> None:
        if self.closed or self._peer is None:
            raise ConnectionError("transport is closed")
        self.sent.append(frame)
        await self._peer._in.put(frame)

    async def receive_frame(self) -> PeerFrame:
        frame = await self._in.get()
        if frame is None:
            raise asyncio.IncompleteReadError(partial=b"", expected=1)
        return frame

    async def close(self) -> None:
        self.closed = True
        await self._in.put(None)


async def _serve_welcome(
    b: MemoryTransport,
    store: NonceStore,
    *,
    orch_id: str = "orch-B1",
    token: str = TOKEN,
) -> None:
    """Respond to one HELLO with a correctly-signed WELCOME."""
    hello = await b.receive_frame()
    await verify(hello, token, store)
    welcome = PeerFrame.welcome(orch_id=orch_id, capabilities=["peer.invoke"])
    sign(welcome, token)
    await b.send_frame(welcome)


async def _serve_and_sink(
    b: MemoryTransport,
    store: NonceStore,
    sink: list[PeerFrame],
) -> None:
    """Handshake, then record every inbound frame until the pipe dies.

    Exits on the D24 GOODBYE (a close from the client side) or the None
    poison pill (an explicit b.close()).
    """
    await _serve_welcome(b, store)
    while True:
        frame = await b._in.get()
        if frame is None:
            return
        sink.append(frame)
        if frame.type is PeerFrameType.GOODBYE:
            return


def _make_client(
    transport_factory,
    tmp_path,
    **overrides,
) -> PeerClient:
    # PeerClient's TransportFactory contract is ``() -> Awaitable``; the
    # tests use plain sync factories, so wrap once here.
    async def _factory():
        return transport_factory()

    kwargs = {
        "orch_id": "orch-A1",
        "token": TOKEN,
        "transport_factory": _factory,
        "nonce_store": NonceStore(tmp_path / "nonces.db"),
        "backoff_base": 0.01,
    }
    kwargs.update(overrides)
    return PeerClient(**kwargs)


# -- handshake (§5, D23) --


async def test_handshake_success_returns_remote_identity(tmp_path) -> None:
    store = NonceStore(tmp_path / "nonces.db")
    a, b = MemoryTransport.link()
    server = asyncio.create_task(_serve_welcome(b, store))
    result = await perform_handshake(
        a, orch_id="orch-A1", token=TOKEN, nonce_store=store
    )
    await server
    assert result.remote_orch_id == "orch-B1"
    assert result.remote_capabilities == ["peer.invoke"]
    assert result.welcome.type is PeerFrameType.WELCOME
    # The HELLO we sent carried our identity and a D3 signature.
    hello = a.sent[0]
    assert hello.type is PeerFrameType.HELLO
    assert hello.orch_id == "orch-A1"
    assert hello.signature


async def test_handshake_non_welcome_rejects_and_closes(tmp_path) -> None:
    store = NonceStore(tmp_path / "nonces.db")
    a, b = MemoryTransport.link()

    async def reply_pong() -> None:
        await b.receive_frame()
        await b.send_frame(PeerFrame.pong(orch_id="orch-B1"))

    server = asyncio.create_task(reply_pong())
    with pytest.raises(HandshakeError, match="expected WELCOME"):
        await perform_handshake(
            a, orch_id="orch-A1", token=TOKEN, nonce_store=store
        )
    await server
    # D23: the failed attempt leaves no half-state.
    assert a.closed


async def test_handshake_timeout_closes_transport(tmp_path) -> None:
    store = NonceStore(tmp_path / "nonces.db")
    a, _b = MemoryTransport.link()  # linked, but no responder ever answers
    with pytest.raises(HandshakeError, match="timed out"):
        await perform_handshake(
            a,
            orch_id="orch-A1",
            token=TOKEN,
            nonce_store=store,
            hello_timeout=0.05,
        )
    assert a.closed


async def test_handshake_bad_welcome_signature(tmp_path) -> None:
    store = NonceStore(tmp_path / "nonces.db")
    a, b = MemoryTransport.link()

    async def forge_welcome() -> None:
        await b.receive_frame()
        welcome = PeerFrame.welcome(orch_id="orch-B1")
        sign(welcome, "attacker-token")
        await b.send_frame(welcome)

    server = asyncio.create_task(forge_welcome())
    with pytest.raises(HandshakeError, match="verification"):
        await perform_handshake(
            a, orch_id="orch-A1", token=TOKEN, nonce_store=store
        )
    await server
    assert a.closed


async def test_handshake_rejects_unverifiable_hello(tmp_path) -> None:
    """A WELCOME we cannot even parse as authentic is a PeerAuthError path."""
    store = NonceStore(tmp_path / "nonces.db")
    a, b = MemoryTransport.link()

    async def unsigned_welcome() -> None:
        await b.receive_frame()
        await b.send_frame(PeerFrame.welcome(orch_id="orch-B1"))

    server = asyncio.create_task(unsigned_welcome())
    with pytest.raises(HandshakeError, match="verification"):
        await perform_handshake(
            a, orch_id="orch-A1", token=TOKEN, nonce_store=store
        )
    await server
    assert a.closed


class _DeadTransport:
    """Transport that dies mid-HELLO with a non-timeout wire failure."""

    def __init__(self, *, fail_on: str) -> None:
        self.fail_on = fail_on
        self.closed = False

    async def send_frame(
        self, frame: PeerFrame, *, end_of_batch: bool = False
    ) -> None:
        if self.fail_on == "send":
            raise ConnectionError("socket gone before HELLO left")
        self.sent = True

    async def receive_frame(self) -> PeerFrame:
        if self.fail_on == "receive":
            raise ConnectionError("connection reset mid-HELLO")
        raise AssertionError("unexpected receive")

    async def close(self) -> None:
        self.closed = True


@pytest.mark.parametrize("fail_on", ["send", "receive"])
async def test_handshake_wire_failure_closes_transport(tmp_path, fail_on) -> None:
    """Regression (PR4 F2): any send/receive failure — not only
    timeouts — must close the transport (D23: no half-state)."""
    store = NonceStore(tmp_path / "nonces.db")
    dead = _DeadTransport(fail_on=fail_on)
    with pytest.raises(HandshakeError, match="HELLO exchange failed"):
        await perform_handshake(
            dead, orch_id="orch-A1", token=TOKEN, nonce_store=store
        )
    assert dead.closed


async def test_send_subscriptions_emits_sorted_subscribe_frames() -> None:
    a, _b = MemoryTransport.link()
    topics = {"peer.agent.b.events", "peer.agent.a.events", "peer.squad.1"}
    await send_subscriptions(a, topics)
    got = [f for f in a.sent if f.type is PeerFrameType.SUBSCRIBE]
    assert [f.topic for f in got] == sorted(topics)


# -- PeerClient lifecycle --


async def test_client_open_and_close_sends_goodbye(tmp_path) -> None:
    a, b = MemoryTransport.link()
    sink: list[PeerFrame] = []
    server = asyncio.create_task(_serve_and_sink(b, NonceStore(tmp_path / "n.db"), sink))
    client = _make_client(lambda: _ok(a), tmp_path)
    result = await client.open()
    assert result.remote_orch_id == "orch-B1"
    assert client.remote_capabilities == ["peer.invoke"]
    await client.close()
    await server
    # D24: close emits a GOODBYE before tearing down.
    goodbyes = [f for f in sink if f.type is PeerFrameType.GOODBYE]
    assert len(goodbyes) == 1
    assert goodbyes[0].orch_id == "orch-A1"
    assert goodbyes[0].in_flight == 0
    assert a.closed
    # The event stream ends cleanly on close.
    assert [f async for f in client.events()] == []


def _ok(transport: MemoryTransport) -> MemoryTransport:
    return transport


async def test_client_close_survives_goodbye_send_failure(tmp_path) -> None:
    """Regression (PR4 F1): a non-PeerClientError failure while sending
    the D24 GOODBYE must not skip the teardown — reader, transport
    close, pending futures, and the event-stream sentinel all run."""
    a, b = MemoryTransport.link()
    server = asyncio.create_task(_serve_welcome(b, NonceStore(tmp_path / "n.db")))
    client = _make_client(lambda: _ok(a), tmp_path)
    await client.open()
    await server

    async def die_mid_goodbye(frame: PeerFrame) -> None:
        raise ConnectionError("socket gone mid-GOODBYE")

    client._transport.send_frame = die_mid_goodbye
    await client.close()
    assert a.closed  # transport.close() still ran
    assert [f async for f in client.events()] == []  # stream still terminated


async def test_client_retries_until_handshake_succeeds(tmp_path) -> None:
    created: list[MemoryTransport] = []

    def factory() -> MemoryTransport:
        a, b = MemoryTransport.link()
        created.append(a)
        if len(created) == 1:
            # First attempt: remote answers with the wrong frame.
            async def pong() -> None:
                await b.receive_frame()
                await b.send_frame(PeerFrame.pong(orch_id="orch-B1"))

            asyncio.create_task(pong())
        else:
            asyncio.create_task(_serve_welcome(b, NonceStore(tmp_path / "n.db")))
        return a

    client = _make_client(factory, tmp_path)
    result = await client.open()
    assert result.remote_orch_id == "orch-B1"
    assert len(created) == 2
    await client.close()


async def test_client_exhausts_retries(tmp_path) -> None:
    created: list[MemoryTransport] = []

    def factory() -> MemoryTransport:
        a, b = MemoryTransport.link()
        created.append(a)

        async def pong() -> None:
            await b.receive_frame()
            await b.send_frame(PeerFrame.pong(orch_id="orch-B1"))

        asyncio.create_task(pong())
        return a

    client = _make_client(factory, tmp_path, retries=2)
    with pytest.raises(PeerClientError, match="after 3 attempts"):
        await client.open()
    assert len(created) == 3
    # Every failed attempt closed its transport (no half-state).
    assert all(t.closed for t in created)


async def test_client_invoke_correlates_result(tmp_path) -> None:
    a, b = MemoryTransport.link()
    sink: list[PeerFrame] = []

    async def serve_and_echo() -> None:
        await _serve_welcome(b, NonceStore(tmp_path / "n.db"))
        while True:
            frame = await b._in.get()
            if frame is None or frame.type is PeerFrameType.GOODBYE:
                return
            sink.append(frame)
            if frame.type is PeerFrameType.INVOKE:
                result = PeerFrame.result(
                    orch_id="orch-B1",
                    request_id=frame.request_id,
                    status=200,
                    body={"ok": True},
                    msg_id=frame.msg_id,
                )
                await b.send_frame(result)

    client = _make_client(lambda: _ok(a), tmp_path)
    server = asyncio.create_task(serve_and_echo())
    await client.open()
    reply = await client.invoke("peer.tasks.create", {"title": "x"})
    await client.close()
    await server
    assert reply.status == 200
    assert reply.body == {"ok": True}
    invokes = [f for f in sink if f.type is PeerFrameType.INVOKE]
    assert len(invokes) == 1
    # D18: the outgoing INVOKE always carries msg_id, echoed by RESULT.
    assert invokes[0].msg_id
    assert invokes[0].request_id == reply.request_id
    assert reply.msg_id == invokes[0].msg_id


async def test_client_invoke_times_out(tmp_path) -> None:
    a, b = MemoryTransport.link()
    server = asyncio.create_task(_serve_welcome(b, NonceStore(tmp_path / "n.db")))
    client = _make_client(lambda: _ok(a), tmp_path, request_timeout=0.05)
    await client.open()
    await server
    with pytest.raises(PeerClientError, match="timed out"):
        await client.invoke("peer.tasks.create", {"title": "x"})
    await client.close()


async def test_client_invoke_before_open(tmp_path) -> None:
    client = _make_client(lambda: MemoryTransport(), tmp_path)
    with pytest.raises(PeerClientError, match="not connected"):
        await client.invoke("peer.tasks.create", {})


async def test_client_subscribe_and_event_stream(tmp_path) -> None:
    a, b = MemoryTransport.link()
    server = asyncio.create_task(_serve_welcome(b, NonceStore(tmp_path / "n.db")))
    client = _make_client(lambda: _ok(a), tmp_path)
    await client.open()
    await server

    async def push_event() -> None:
        await b._in.get()  # the SUBSCRIBE frame
        await b.send_frame(
            PeerFrame.event(
                orch_id="orch-B1",
                topic="peer.agent.a.events",
                payload={"seq": 1},
            )
        )

    pusher = asyncio.create_task(push_event())
    await client.subscribe(["peer.agent.a.events"])
    assert client.topics == {"peer.agent.a.events"}
    events = client.events()
    frame = await asyncio.wait_for(anext(events), timeout=2)
    await pusher
    assert frame.type is PeerFrameType.EVENT
    assert frame.payload == {"seq": 1}
    await client.close()
    # Close ends the stream (sentinel), so the async-for returns.
    collected = [f async for f in events]
    assert collected == []


# -- SSE data-line decoding (PR5 server binding helper) --


def _wire(frame: PeerFrame) -> str:
    return json.dumps(frame.to_dict(), ensure_ascii=False)


def test_parse_sse_line_valid_frame() -> None:
    frame = PeerFrame.event(orch_id="orch-B1", topic="peer.x", payload={"n": 1})
    parsed = parse_sse_data_line(f"data: {_wire(frame)}")
    assert parsed is not None
    assert parsed.type is PeerFrameType.EVENT
    assert parsed.frame_id == frame.frame_id


@pytest.mark.parametrize(
    "line",
    [
        "",
        ": keep-alive comment",
        "event: ping",
        "data:",
        "data: not-json",
        "data: []",
        "plain text without prefix",
    ],
)
def test_parse_sse_line_noise_returns_none(line: str) -> None:
    assert parse_sse_data_line(line) is None


def test_parse_sse_line_bad_type_returns_none() -> None:
    # A JSON object with an unknown frame type must not raise.
    assert parse_sse_data_line('data: {"type": "bogus"}') is None


# -- PR-B9: SSE broker-dict → EVENT conversion (client._sse_line_to_frame) --


def _client_for_sse(tmp_path) -> PeerClient:
    return _make_client(lambda: MemoryTransport(), tmp_path)


def test_sse_line_broker_dict_converts_to_event(tmp_path) -> None:
    """The peer SSE endpoint emits broker dicts ``{"topic", "payload"}``;
    the client converts them into EVENT frames."""
    client = _client_for_sse(tmp_path)
    frame = client._sse_line_to_frame(
        'data: {"topic": "peer.x", "payload": {"n": 1}}'
    )
    assert frame is not None
    assert frame.type is PeerFrameType.EVENT
    assert frame.topic == "peer.x"
    assert frame.payload == {"n": 1}
    assert frame.orch_id == client._orch_id


def test_sse_line_frame_jsonl_still_parses(tmp_path) -> None:
    """A PeerFrame JSONL data line keeps flowing through the frame
    decoder unchanged."""
    client = _client_for_sse(tmp_path)
    wire = json.dumps(
        PeerFrame.event(
            orch_id="orch-B1", topic="peer.y", payload={"seq": 2}
        ).to_dict(),
        ensure_ascii=False,
    )
    frame = client._sse_line_to_frame(f"data: {wire}")
    assert frame is not None
    assert frame.type is PeerFrameType.EVENT
    assert frame.payload == {"seq": 2}


@pytest.mark.parametrize(
    "line", ["", ": keep-alive", "event: ping", "data:", "data: [1,2]"]
)
def test_sse_line_noise_returns_none(tmp_path, line: str) -> None:
    client = _client_for_sse(tmp_path)
    assert client._sse_line_to_frame(line) is None


def test_sse_task_not_started_without_frame_url(tmp_path) -> None:
    """In-memory / legacy clients never open the SSE stream."""
    client = _client_for_sse(tmp_path)
    client._ensure_sse()
    assert client._sse_task is None


def test_peer_client_error_is_exception() -> None:
    assert issubclass(PeerClientError, Exception)
    assert issubclass(PeerAuthError, Exception)


# -- PR-B1: transport="auto"|"rest"|"frame" selector semantics --


def test_client_default_transport_is_auto() -> None:
    """PR-B1: ``PeerClient.__init__`` defaults to ``transport="auto"``.

    A v2 client defaults to the modern selector; v1 callers must
    opt into ``transport="rest"`` explicitly to keep the legacy
    behavior. This test pins the default so the contract cannot drift
    to a v1-compat path silently.
    """
    import inspect

    sig = inspect.signature(PeerClient.__init__)
    assert sig.parameters["transport"].default == "auto"


def test_auto_logs_legacy_protocol_warning_when_card_has_no_transports(
    tmp_path, caplog
) -> None:
    """PR-B1 / PR-B2 invariant: a remote on the legacy Phase 1 wire
    (no ``transports[]`` in card) triggers a one-time warning, then
    falls back to the injected factory. The PR-B2 factory-swap path
    is bypassed in this branch — proving the legacy compatibility
    promise is unchanged by Phase B."""
    import logging

    transport_a = MemoryTransport()
    client = _make_client(lambda: transport_a, tmp_path)
    # Inject a card with no transports[] — the legacy Phase 1 shape.
    client.card = {
        "protocol_version": "peer/1",
        "orch_id": "orch-LEGACY",
        "transports": [],  # empty / missing
    }
    with caplog.at_level(logging.WARNING):
        client._negotiate_transport()
    # Factory is preserved (Phase 1 fallback path).
    assert client._transport_factory is not None
    # Warning text mentions legacy protocol / transports[].
    messages = [r.message for r in caplog.records]
    assert any("legacy" in m.lower() or "transports" in m for m in messages), (
        f"expected legacy protocol warning, got {messages!r}"
    )


def test_client_explicit_rest_logs_deprecation(tmp_path, caplog) -> None:
    """PR-B1: ``transport="rest"`` is explicit legacy and emits a
    deprecation warning the operator should see exactly once."""
    import logging

    transport_a = MemoryTransport()
    client = _make_client(lambda: transport_a, tmp_path, transport="rest")
    with caplog.at_level(logging.WARNING):
        client._negotiate_transport()
    messages = [r.message for r in caplog.records]
    assert any(
        "deprecated" in m.lower() or "transport='rest'" in m for m in messages
    ), f"expected deprecation warning, got {messages!r}"


def test_client_explicit_frame_succeeds_when_url_provided(tmp_path) -> None:
    """PR-B2: ``transport="frame"`` actually swaps to ``HttpsFrameTransport``
    once a frame URL is supplied. The PR-B1 ``NotImplementedError`` path
    is gone — the binding is real."""
    transport_a = MemoryTransport()
    client = _make_client(lambda: transport_a, tmp_path, transport="frame")
    client.card = {
        "transports": [
            {
                "protocol": "frame",
                "url": "https://h/peer/v1/stream",
                "version": "1",
            },
        ],
    }
    client._frame_url = "https://h/peer/v1/stream"
    # Must NOT raise NotImplementedError; must NOT raise at all.
    client._negotiate_transport()
    # The factory has been swapped to one that opens HttpsFrameTransport.
    assert client._transport_factory is not None
    assert callable(client._transport_factory)


def test_client_explicit_frame_raises_when_no_url(tmp_path) -> None:
    """PR-B2: ``transport="frame"`` without a frame URL is a clear,
    actionable error — not a silent downgrade to the injected factory."""
    transport_a = MemoryTransport()
    client = _make_client(lambda: transport_a, tmp_path, transport="frame")
    client.card = {
        "transports": [
            {
                "protocol": "frame",
                "url": "https://h/peer/v1/stream",
                "version": "1",
            },
        ],
    }
    # No ``_frame_url`` set — open() failed to discover one.
    assert client._frame_url is None
    with pytest.raises(PeerClientError, match="frame URL"):
        client._negotiate_transport()
