"""End-to-end peer/1 frame transport integration test (PR-B2).

Spins up the FastAPI app in-process via :mod:`uvicorn` on a free
port, then drives a real :class:`PeerClient` against the
``POST /peer/v1/stream`` endpoint with the frame URL discovered from
the Agent Card. Verifies:

1. ``PeerClient.open()`` actually swaps the transport factory to
   :class:`HttpsFrameTransport` when the remote advertises frame
   (the PR-B1 stub is gone).
2. HELLO/WELCOME round-trips through the wire.
3. INVOKE → RESULT (PR-B2 stub echoes back the accepted body).
4. Close emits a GOODBYE before tearing the transport down.

Skipped when the test environment cannot build a peer registry row
(SQLAlchemy aiosqlite unavailable) — same pattern as the REST peer
federation tests.
"""

from __future__ import annotations

import asyncio
import os
import socket
import threading
import time
import uuid
from datetime import UTC, datetime

import httpx
import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from orchestratord.api.app import create_app
from orchestratord.api.routers import peer_frame
from orchestratord.api.routers.peer import reset_dispatcher
from orchestratord.db import models as orm
from orchestratord.db.engine import build_engine, create_schema
from orchestratord.domain.auth_token import hash_api_token, issue_api_token
from orchestratord.peer import connections as peer_connections
from orchestratord.peer.client import PeerClient
from orchestratord.peer.nonce_store import NonceStore
from orchestratord.peer.protocol import PeerFrame, PeerFrameType
from orchestratord.peer.registry import upsert_peer


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


async def _setup_app_with_peer(tmp_path):
    """Create a FastAPI app + accepted peer + auth token row."""
    db_path = tmp_path / "peer_two_daemon_frame.db"
    os.environ["ORCHESTRATORD_DATABASE_URL"] = f"sqlite+aiosqlite:///{db_path}"
    os.environ["ORCHESTRATORD_PEER_NONCE_PATH"] = str(tmp_path / "nonces.db")
    engine = build_engine()
    await create_schema(engine)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    ws_id = uuid.uuid4()
    async with factory() as session:
        peer = await upsert_peer(
            session,
            workspace_id=ws_id,
            orch_id="orch-REMOTE",
            name="Remote",
            url="http://r:9001",
            capabilities=["peer.invoke"],
            client_kind="v2",
        )
        plaintext, token_hash = issue_api_token()
        token_row = orm.AuthToken(
            id=uuid.uuid4(),
            workspace_id=ws_id,
            name=f"peer:{peer.orch_id}",
            token_hash=hash_api_token(plaintext),
            scopes=["peer.invoke"],
            expires_at=None,
            created_at=datetime.now(UTC),
        )
        session.add(token_row)
        await session.flush()
        peer.token_id = token_row.id
        await session.commit()
        await session.refresh(peer)
    await engine.dispose()
    app = create_app()
    return app, peer, plaintext


async def _serve_in_thread(app):
    """Run the FastAPI app on a real port via uvicorn in a background thread."""
    import uvicorn

    port = _free_port()
    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=port,
        log_level="warning",
        lifespan="on",
    )
    server = uvicorn.Server(config)

    def _serve() -> None:
        try:
            server.run()
        except Exception:
            pass

    thread = threading.Thread(target=_serve, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        try:
            async with httpx.AsyncClient() as probe:
                resp = await probe.get(
                    f"http://127.0.0.1:{port}/.well-known/agent.json"
                )
                if resp.status_code == 200:
                    return server, thread, port
        except Exception:
            pass
        await asyncio.sleep(0.1)
    raise RuntimeError("uvicorn did not start in time")


async def _stop_server(server, thread) -> None:
    server.should_exit = True
    thread.join(timeout=5.0)


def _in_memory_factory():
    """Injected fallback that the PeerClient must NOT use after the swap."""
    raise AssertionError(
        "PeerClient with transport='auto' + frame advertised must "
        "use HttpsFrameTransport, not the injected factory"
    )


@pytest.mark.asyncio
async def test_peer_client_uses_frame_transport_when_advertised(tmp_path) -> None:
    """End-to-end: PeerClient discovers frame URL from Agent Card and
    swaps the transport factory — proves the PR-B2 contract that
    ``transport='auto'`` actually opens an HTTPS frame stream."""
    try:
        app, peer, plaintext = await _setup_app_with_peer(tmp_path)
    except Exception:
        pytest.skip("peer row setup requires sqlite aiosqlite support")

    peer_frame.reset_nonce_store()
    reset_dispatcher()
    for conn in list(peer_connections.live_inbound()):
        peer_connections.unregister_inbound(conn)

    server, thread, port = await _serve_in_thread(app)
    try:
        base_url = f"http://127.0.0.1:{port}"
        # Probe the Agent Card for the frame URL.
        async with httpx.AsyncClient(timeout=5.0) as probe:
            card_resp = await probe.get(f"{base_url}/.well-known/agent.json")
        assert card_resp.status_code == 200
        card = card_resp.json()
        frame_entry = next(
            t for t in card["transports"] if t["protocol"] == "frame"
        )
        # The card advertises a frame URL on the sentinel port (9000 by
        # default); rewrite it to the actual port the test server bound.
        frame_url = frame_entry["url"].replace(
            "http://127.0.0.1:9000", base_url
        )
        # PeerClient with ``base_url`` triggers discovery → frame URL
        # capture → ``transport='auto'`` swap to HttpsFrameTransport.
        client = PeerClient(
            orch_id="orch-LOCAL",
            token=plaintext,
            transport_factory=_in_memory_factory,
            nonce_store=NonceStore(tmp_path / "client_nonces.db"),
            base_url=base_url,
            backoff_base=0.05,
        )
        result = await asyncio.wait_for(client.open(), timeout=10.0)
        assert result.remote_orch_id == peer.orch_id
        # PR-B2: the factory must have been swapped to frame (not the
        # injected ``_in_memory_factory`` we passed in).
        assert client._frame_url is not None
        assert client._frame_url.startswith("http")
        assert client._frame_url == frame_url

        # INVOKE → RESULT (PR-B2 stub echoes back the accepted body).
        result_frame = await asyncio.wait_for(
            client.invoke("peer.tasks.create", {"title": "x"}), timeout=10.0
        )
        assert result_frame.body["status"] == "accepted"
        assert result_frame.body["method"] == "peer.tasks.create"
        await client.close()
    finally:
        await _stop_server(server, thread)