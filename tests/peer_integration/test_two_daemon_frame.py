"""End-to-end peer/1 frame transport integration test (PR-B2 + PR-B3).

Spins up the FastAPI app in-process via :mod:`uvicorn` on a free
port, then drives a real :class:`PeerClient` against the
``POST /peer/v1/stream`` endpoint with the frame URL discovered from
the Agent Card. Verifies:

1. ``PeerClient.open()`` actually swaps the transport factory to
   :class:`HttpsFrameTransport` when the remote advertises frame
   (the PR-B1 stub is gone).
2. HELLO/WELCOME round-trips through the wire.
3. INVOKE → RESULT for a **PR-B3 routed method**
   (``POST /api/sessions/{session_id}/messages``) — the response body
   carries ``message_id`` + ``seq``, and the row + AuditLogEntry
   actually land in the DB (proves dispatch table is wired end-to-end,
   not just the PR-B2 stub echo).
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
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select
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
    """Create a FastAPI app + accepted peer + auth token row + seed session.

    PR-B3 integration drives ``POST /api/sessions/{session_id}/messages``,
    which requires the target session row to exist before the INVOKE
    lands (the handler returns 404 if not). Seed one inside the same
    SQLite file so the handler's `repos.session.get` finds it, and
    return the SQLite path so the caller can open a fresh engine on
    it after the invoke to verify the row + audit_log actually landed.
    """
    db_path = tmp_path / "peer_two_daemon_frame.db"
    os.environ["ORCHESTRATORD_DATABASE_URL"] = f"sqlite+aiosqlite:///{db_path}"
    os.environ["ORCHESTRATORD_PEER_NONCE_PATH"] = str(tmp_path / "nonces.db")
    engine = build_engine()
    await create_schema(engine)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    ws_id = uuid.uuid4()
    session_id = uuid.uuid4()
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
        # Seed a Session so the routed `POST /api/sessions/{session_id}/
        # messages` handler can find it (PR-B3 wired handler returns 404
        # otherwise).
        session_row = orm.Session(
            id=session_id,
            workspace_id=ws_id,
            issue_id=None,
            agent_id=None,
            run_id=None,
            mode="chat",
            status="active",
            created_at=datetime.now(UTC),
        )
        session.add(session_row)
        await session.commit()
        await session.refresh(peer)
    await engine.dispose()
    app = create_app()
    return app, peer, plaintext, session_id, Path(db_path)


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
    swaps the transport factory, then drives a PR-B3 routed INVOKE
    (``POST /api/sessions/{session_id}/messages``) that lands a
    ``Message`` row + ``AuditLogEntry`` in the SQLite DB.

    Proves both contracts:

    * PR-B2: ``transport='auto'`` actually swaps to
      :class:`HttpsFrameTransport` (the injected in-memory factory is
      never used).
    * PR-B3: the dispatch table is wired end-to-end — a routed method
      returns the real handler's RESULT body and the side effects
      (DB write + audit) reach the disk, not just an in-memory stub.
    """
    try:
        app, peer, plaintext, session_id, db_path = await _setup_app_with_peer(
            tmp_path
        )
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

        # PR-B3: drive a real routed INVOKE. ``handle_sessions_message_post``
        # expects ``{session_id, role, content}`` and returns
        # ``{message_id, seq}``.
        message_content = "hi from peer integration test"
        result_frame = await asyncio.wait_for(
            client.invoke(
                "POST /api/sessions/{session_id}/messages",
                {
                    "session_id": str(session_id),
                    "role": "user",
                    "content": message_content,
                },
            ),
            timeout=10.0,
        )
        assert "message_id" in result_frame.body, result_frame.body
        assert "seq" in result_frame.body, result_frame.body
        assert isinstance(result_frame.body["seq"], int)
        written_message_id = result_frame.body["message_id"]
        await client.close()
    finally:
        await _stop_server(server, thread)

    # PR-B3 invariant: the handler's side effect (Message row +
    # AuditLogEntry with invited_by_orch_id == peer.orch_id) landed on
    # disk. Open a fresh engine on the same SQLite file to read what
    # the server committed (the server's own engine has been disposed
    # by ``_stop_server`` → uvicorn shutdown).
    os.environ["ORCHESTRATORD_DATABASE_URL"] = f"sqlite+aiosqlite:///{db_path}"
    verify_engine = build_engine()
    try:
        verify_factory = async_sessionmaker(verify_engine, expire_on_commit=False)
        async with verify_factory() as verify_session:
            msg_row = await verify_session.get(orm.Message, uuid.UUID(written_message_id))
            assert msg_row is not None, "Message row missing from DB"
            assert str(msg_row.session_id) == str(session_id)
            assert msg_row.role == "user"
            assert msg_row.content == message_content
            assert msg_row.author_label == peer.orch_id
            audit_rows = (
                await verify_session.execute(
                    select(orm.AuditLogEntry).where(
                        orm.AuditLogEntry.invited_by_peer_call_id
                        == written_message_id
                    )
                )
            ).scalars().all()
            assert len(audit_rows) == 1, (
                f"expected exactly one audit row, got {len(audit_rows)}"
            )
            assert audit_rows[0].action == "peer.invoke.message"
            assert audit_rows[0].invited_by_orch_id == peer.orch_id
    finally:
        # Reset the env var so subsequent tests don't pin this DB URL.
        os.environ.pop("ORCHESTRATORD_DATABASE_URL", None)
        await verify_engine.dispose()