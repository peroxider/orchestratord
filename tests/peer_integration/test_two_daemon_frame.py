"""End-to-end peer/1 frame transport integration test (PR-B2/B3/B9).

Spins up the FastAPI app in-process via :mod:`uvicorn` on a free
port, then drives a real :class:`PeerClient` against the
``POST /peer/v1/stream`` endpoint with the frame URL discovered from
the Agent Card. Verifies:

1. ``PeerClient.open()`` swaps the transport factory to
   :class:`HttpsFrameTransport` when the remote advertises frame.
2. HELLO/WELCOME round-trips through the wire (one batch POST).
3. INVOKE → RESULT for a **routed method**
   (``POST /api/sessions/{session_id}/messages``) — ``message_id`` +
   ``seq`` in the body, and the ``Message`` row + ``AuditLogEntry``
   actually land in PostgreSQL.
4. SUBSCRIBE registers the topic server-side (PR-B9 per-peer topic
   registry) and unsolicited EVENTs arrive over the SSE endpoint —
   the batch-POST binding cannot push, so this is the only EVENT path.
5. Close emits a D24 GOODBYE and clears the server-side registry.

PR-B9 (D5): backed by the shared ``orchestratord_test`` PostgreSQL —
SQLite cannot compile the JSONB schema, which left this canary
permanently skipped while the PR-B7 body-after-response breakage hid
behind buffering TestClients. Skipped when PostgreSQL is unreachable
(same pattern as ``tests/db_integration``).
"""

from __future__ import annotations

import asyncio
import socket
import threading
import time
import uuid
from datetime import UTC, datetime

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from orchestratord.api.app import create_app
from orchestratord.api.deps import reset_peer_rate_bucket
from orchestratord.api.realtime import get_broker, reset_broker
from orchestratord.api.routers import peer_frame
from orchestratord.api.routers.peer import reset_dispatcher
from orchestratord.db import models as orm
from orchestratord.db.engine import build_engine, create_schema
from orchestratord.domain.auth_token import hash_api_token, issue_api_token
from orchestratord.peer import connections as peer_connections
from orchestratord.peer import topic_registry
from orchestratord.peer.client import PeerClient
from orchestratord.peer.nonce_store import NonceStore
from orchestratord.peer.protocol import PeerFrame, PeerFrameType
from orchestratord.peer.registry import set_peer_status, upsert_peer


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


async def _setup_app_with_peer(dsn, tmp_path, monkeypatch, port):
    """Create a FastAPI app + accepted peer + auth token row + seed session.

    PR-B9 (D5): the env var is set **before** ``build_engine()`` /
    ``create_app()`` so the app's lifespan engine binds to the scratch
    PostgreSQL — SQLite cannot compile the JSONB models. The peer row
    must be flipped to ``accepted`` (``upsert_peer`` defaults to
    PENDING, which ``require_peer_auth`` rejects).

    ``ORCHESTRATORD_PEER_LISTEN`` pins the Agent Card's advertised
    origin to the test server's port — the client discovers the frame
    URL from the card itself, so the card must not advertise the
    sentinel port.
    """
    monkeypatch.setenv("ORCHESTRATORD_PEER_LISTEN", f"127.0.0.1:{port}")
    monkeypatch.setenv("ORCHESTRATORD_DATABASE_URL", dsn)
    monkeypatch.setenv(
        "ORCHESTRATORD_PEER_NONCE_PATH", str(tmp_path / "nonces.db")
    )
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
        peer = await set_peer_status(session, peer, "accepted")
        plaintext, _token_hash = issue_api_token()
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
        # messages` handler can find it (returns 404 otherwise).
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
    # The API layer caches its session factory for the process lifetime;
    # earlier tests in the same run may have bound it to their own DB.
    # Clear it so create_app() rebuilds against *this* DSN, and clear it
    # again at teardown so later tests don't inherit this engine.
    from orchestratord.api.db import reset_session_factory

    reset_session_factory()
    app = create_app()
    return app, peer, plaintext, session_id


async def _serve_in_thread(app, port):
    """Run the FastAPI app on the pre-selected port in a background thread."""
    import uvicorn

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
                    return server, thread
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


@pytest.mark.database
@pytest.mark.asyncio
async def test_peer_client_uses_frame_transport_when_advertised(
    tmp_path, peer_pg, monkeypatch
) -> None:
    """End-to-end over a real socket: discovery → frame-transport swap →
    batch-POST HELLO → routed INVOKE → SUBSCRIBE/SSE EVENT push →
    D24 GOODBYE. This is the PR-B9 canary: a true uvicorn wire, not a
    buffering TestClient."""
    port = _free_port()
    app, peer, plaintext, session_id = await _setup_app_with_peer(
        peer_pg, tmp_path, monkeypatch, port
    )

    peer_frame.reset_nonce_store()
    reset_dispatcher()
    reset_broker()
    reset_peer_rate_bucket()
    topic_registry.reset_peer_topics()
    for conn in list(peer_connections.live_inbound()):
        peer_connections.unregister_inbound(conn)

    server, thread = await _serve_in_thread(app, port)
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
        # The card advertises the test server's own origin (pinned via
        # ORCHESTRATORD_PEER_LISTEN), so the client's discovery lands on
        # the live port without rewriting.
        frame_url = frame_entry["url"]
        assert frame_url == f"{base_url}/peer/v1/stream"
        # The client identifies as the registered peer — the SSE event
        # endpoint (PR-B9) only serves /events for the authenticated
        # peer's own orch_id.
        client = PeerClient(
            orch_id=peer.orch_id,
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

        # Routed INVOKE: ``handle_sessions_message_post`` expects
        # ``{session_id, role, content}`` and returns ``{message_id, seq}``.
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

        # PR-B9: SUBSCRIBE registers the topic server-side, and
        # unsolicited EVENTs arrive over the SSE endpoint. Both the
        # SUBSCRIBE batch POST and the SSE connection are asynchronous
        # (batches flush as fire-and-forget tasks) — poll until the
        # registry reflects the topic, then poll-publish until the
        # broker reports a live SSE subscriber.
        topic = "peer.probe.events"
        await client.subscribe([topic])
        deadline = time.monotonic() + 5.0
        while (
            topic_registry.get_peer_topics(peer.orch_id) != {topic}
            and time.monotonic() < deadline
        ):
            await asyncio.sleep(0.05)
        assert topic_registry.get_peer_topics(peer.orch_id) == {topic}
        events = client.events()
        broker = get_broker()
        for _ in range(50):
            delivered = await broker.publish(topic, {"n": 0})
            if delivered:
                break
            await asyncio.sleep(0.1)
        event = await asyncio.wait_for(anext(events), timeout=5.0)
        assert event.type is PeerFrameType.EVENT
        assert event.topic == topic
        assert event.payload == {"n": 0}

        await client.close()
        # D24 GOODBYE cleared the server-side registry (PR-B9 D2).
        assert topic_registry.get_peer_topics(peer.orch_id) == set()
        # The event stream ends cleanly on close.
        assert [f async for f in events] == []
    finally:
        await _stop_server(server, thread)
        topic_registry.reset_peer_topics()
        reset_peer_rate_bucket()
        from orchestratord.api.db import reset_session_factory

        reset_session_factory()

    # The handler's side effects (Message row + AuditLogEntry with
    # invited_by_orch_id == peer.orch_id) landed in PostgreSQL. Read
    # them with a fresh engine (the server's own engine was disposed by
    # the uvicorn shutdown above).
    verify_engine = build_engine(peer_pg)
    try:
        verify_factory = async_sessionmaker(
            verify_engine, expire_on_commit=False
        )
        async with verify_factory() as verify_session:
            msg_row = await verify_session.get(
                orm.Message, uuid.UUID(written_message_id)
            )
            assert msg_row is not None, "Message row missing from DB"
            assert str(msg_row.session_id) == str(session_id)
            assert msg_row.role == "user"
            assert msg_row.content == message_content
            assert msg_row.author_label == peer.orch_id
            audit_rows = (
                await verify_session.execute(
                    select(orm.AuditLogEntry).where(
                        orm.AuditLogEntry.invited_by_peer_call_id
                        == result_frame.msg_id
                    )
                )
            ).scalars().all()
            # The frame path stitches the audit row to the D18 msg_id
            # (``method_handlers._persist_session_message``), echoed back
            # on the RESULT frame.
            assert len(audit_rows) == 1, (
                f"expected exactly one audit row, got {len(audit_rows)}"
            )
            assert audit_rows[0].action == "peer.invoke.message"
            assert audit_rows[0].invited_by_orch_id == peer.orch_id
    finally:
        await verify_engine.dispose()
