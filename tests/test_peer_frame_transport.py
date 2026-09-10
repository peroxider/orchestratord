"""HttpsFrameTransport contract tests (DESIGN §6.1, PR-B2).

These tests target the client-side ``HttpsFrameTransport`` contract
without driving a full HTTP round-trip — bidirectional chunked POSTs
through ASGITransport are awkward (the ASGI server does not yield the
response stream back through a single ``httpx.AsyncClient.stream``
call). The wire-level HELLO/WELCOME/INVOKE/RESULT round-trip is covered
end-to-end by ``tests/api/test_peer_frame_router.py`` against the real
FastAPI app.

What this file covers:

* ``select_transport_factory`` chooses frame when advertised.
* ``select_transport_factory`` returns the fallback otherwise.
* ``HttpsFrameTransport.close()`` is idempotent (no half-state).
* ``send_subscriptions`` emits one SUBSCRIBE frame per topic.
"""

from __future__ import annotations

import asyncio

import pytest

from orchestratord.peer.protocol import PeerFrame
from orchestratord.peer.transports import HttpsFrameTransport, select_transport_factory

TOKEN = "peer-secret-token"


# -- select_transport_factory --


def test_select_transport_factory_prefers_frame_when_advertised() -> None:
    """Frame advertised in card → factory is *not* the fallback."""
    card = {
        "transports": [
            {
                "protocol": "frame",
                "url": "https://h/peer/v1/stream",
                "version": "1",
            },
            {"protocol": "rest", "url": "https://h", "version": "1"},
        ]
    }

    async def fallback():
        return None

    factory = select_transport_factory(
        card,
        frame_url="https://h/peer/v1/stream",
        orch_id="orch-X",
        token=TOKEN,
        fallback_factory=fallback,
    )
    assert factory is not fallback


def test_select_transport_factory_falls_back_when_only_rest() -> None:
    card = {
        "transports": [
            {"protocol": "rest", "url": "https://h", "version": "1"},
        ]
    }

    async def fallback():
        return None

    factory = select_transport_factory(
        card,
        frame_url=None,
        orch_id="orch-X",
        token=TOKEN,
        fallback_factory=fallback,
    )
    assert factory is fallback


def test_select_transport_factory_falls_back_when_no_card() -> None:
    async def fallback():
        return None

    factory = select_transport_factory(
        None,
        frame_url=None,
        orch_id="orch-X",
        token=TOKEN,
        fallback_factory=fallback,
    )
    assert factory is fallback


def test_select_transport_factory_falls_back_when_no_frame_url() -> None:
    """Frame advertised but no URL captured → falls back rather than crashing."""
    card = {
        "transports": [
            {
                "protocol": "frame",
                "url": "https://h/peer/v1/stream",
                "version": "1",
            },
        ]
    }

    async def fallback():
        return None

    factory = select_transport_factory(
        card,
        frame_url=None,
        orch_id="orch-X",
        token=TOKEN,
        fallback_factory=fallback,
    )
    assert factory is fallback


def test_select_transport_factory_ignores_non_dict_card_entries() -> None:
    """Malformed transports[] entries must not crash the selector."""
    card = {"transports": [None, "frame", {"protocol": "rest"}]}

    async def fallback():
        return None

    factory = select_transport_factory(
        card,
        frame_url=None,
        orch_id="orch-X",
        token=TOKEN,
        fallback_factory=fallback,
    )
    assert factory is fallback


# -- HttpsFrameTransport lifecycle --


async def test_close_is_idempotent_without_connect() -> None:
    """A transport closed without ever connecting must not raise."""
    transport = HttpsFrameTransport(
        url="http://127.0.0.1:1/peer/v1/stream",
        orch_id="orch-X",
        token=TOKEN,
    )
    await transport.close()
    await transport.close()  # second close is a no-op


async def test_send_frame_without_connect_raises() -> None:
    """Send/receive before connect must surface as a clear error."""
    from orchestratord.peer.protocol import PeerFrame

    transport = HttpsFrameTransport(
        url="http://127.0.0.1:1/peer/v1/stream",
        orch_id="orch-X",
        token=TOKEN,
    )
    with pytest.raises(RuntimeError, match="not connected"):
        await transport.send_frame(PeerFrame.ping(orch_id="orch-X"))


async def test_receive_frame_without_connect_raises() -> None:
    transport = HttpsFrameTransport(
        url="http://127.0.0.1:1/peer/v1/stream",
        orch_id="orch-X",
        token=TOKEN,
    )
    with pytest.raises(RuntimeError, match="not connected"):
        await transport.receive_frame()


# -- PR-B9: batch-POST binding --


def _make_mock_transport(
    handler,
) -> HttpsFrameTransport:
    """HttpsFrameTransport wired to an ``httpx.MockTransport`` handler."""
    import httpx

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return HttpsFrameTransport(
        url="https://h/peer/v1/stream",
        orch_id="orch-X",
        token=TOKEN,
        client=client,
    )


async def _drain_inflight(transport: HttpsFrameTransport) -> None:
    while transport._inflight:
        await asyncio.sleep(0)
        for task in list(transport._inflight):
            if task.done():
                await asyncio.gather(task, return_exceptions=True)


async def test_batch_flushes_as_one_post() -> None:
    """Frames buffered without ``end_of_batch`` ship as ONE POST when
    the batch terminator arrives; the response JSONL replays frames
    into ``receive_frame`` in order."""
    import httpx

    from orchestratord.peer.protocol import PeerFrame, PeerFrameType

    requests: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request.read())
        reply = PeerFrame.pong(orch_id="orch-Y")
        return httpx.Response(
            200, content=reply.encode() + b"\n",
        )

    transport = _make_mock_transport(handler)
    await transport._open()
    try:
        await transport.send_frame(PeerFrame.ping(orch_id="orch-X"))
        await transport.send_frame(PeerFrame.ping(orch_id="orch-X"))
        # No POST yet — the batch has not been terminated.
        assert requests == []
        await transport.send_frame(
            PeerFrame.ping(orch_id="orch-X"), end_of_batch=True
        )
        await _drain_inflight(transport)
        # Exactly one request carrying all three JSONL frames.
        assert len(requests) == 1
        body_lines = [ln for ln in requests[0].splitlines() if ln.strip()]
        assert len(body_lines) == 3
        assert PeerFrame.decode(body_lines[0]).type is PeerFrameType.PING
        pong = await transport.receive_frame()
        assert pong.type is PeerFrameType.PONG
        assert pong.orch_id == "orch-Y"
    finally:
        await transport.close()


async def test_two_batches_ship_as_two_posts() -> None:
    """Each terminated batch is its own POST over the shared client."""
    import httpx

    from orchestratord.peer.protocol import PeerFrame

    requests: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request.read())
        return httpx.Response(200, content=b"")

    transport = _make_mock_transport(handler)
    await transport._open()
    try:
        for _ in range(2):
            await transport.send_frame(
                PeerFrame.ping(orch_id="orch-X"), end_of_batch=True
            )
        await _drain_inflight(transport)
        assert len(requests) == 2
    finally:
        await transport.close()


async def test_failed_batch_surfaces_as_runtime_error() -> None:
    """A failed batch POST drops the teardown sentinel; the next
    ``receive_frame`` raises ``RuntimeError`` (session-teardown
    signal, PR-B2 reader semantics)."""
    import httpx

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("peer unreachable")

    transport = _make_mock_transport(handler)
    await transport._open()
    try:
        await transport.send_frame(
            PeerFrame.ping(orch_id="orch-X"), end_of_batch=True
        )
        with pytest.raises(RuntimeError, match="batch request failed"):
            await transport.receive_frame()
    finally:
        await transport.close()


async def test_close_drops_unterminated_batch_without_posting() -> None:
    """A buffered batch never terminated by ``end_of_batch`` is dropped
    on close — no request leaves the client."""
    import httpx

    requests: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request.read())
        return httpx.Response(200, content=b"")

    transport = _make_mock_transport(handler)
    await transport._open()
    await transport.send_frame(PeerFrame.ping(orch_id="orch-X"))
    await transport.close()
    assert requests == []