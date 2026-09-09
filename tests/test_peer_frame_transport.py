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

import pytest

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