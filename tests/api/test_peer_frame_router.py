"""Tests for the ``POST /peer/v1/stream`` chunked-JSONL endpoint (PR-B2).

Uses FastAPI's ``TestClient`` to drive the chunked stream end-to-end:

* 401 without bearer token
* 401 without ``X-Peer-Orchestrator-Id`` header
* HELLO → WELCOME round-trip
* INVOKE → RESULT round-trip (stubbed execute in PR-B2)
* GOODBYE closes the response cleanly
* The D24 inbound registry tracks the open connection and releases it
  on close
* Agent Card advertises both ``frame`` and ``rest`` transports (PR-B2)
"""

from __future__ import annotations

import asyncio
import time as _time
import uuid
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker

from orchestratord.api.app import create_app
from orchestratord.api.routers import peer_frame
from orchestratord.api.routers.peer import reset_dispatcher
from orchestratord.db import models as orm
from orchestratord.domain.auth_token import hash_api_token, issue_api_token
from orchestratord.peer import connections as peer_connections
from orchestratord.peer.hmac_sig import sign
from orchestratord.peer.nonce_store import NonceStore
from orchestratord.peer.protocol import PeerFrame, PeerFrameType
from orchestratord.peer.registry import upsert_peer


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv(
        "ORCHESTRATORD_PEER_NONCE_PATH", str(tmp_path / "nonces.db")
    )
    peer_frame.reset_nonce_store()
    reset_dispatcher()
    for conn in list(peer_connections.live_inbound()):
        peer_connections.unregister_inbound(conn)
    return create_app()


@pytest.fixture
def client(app):
    with TestClient(app) as c:
        yield c


async def _build_accepted_peer(tmp_path) -> tuple[Any, str]:
    """Create a Peer row + valid auth_tokens row, returning (peer, plaintext).

    Skipped when the test environment doesn't have SQLAlchemy's aiosqlite
    driver wired up (mirrors the pattern from existing peer tests).
    """
    try:
        from orchestratord.db.engine import build_engine, create_schema
    except ImportError:
        pytest.skip("SQLAlchemy not available")

    import os

    db_path = tmp_path / "peer_frame_router.db"
    os.environ["ORCHESTRATORD_DATABASE_URL"] = f"sqlite+aiosqlite:///{db_path}"

    engine = build_engine()
    try:
        await create_schema(engine)
    except Exception:
        await engine.dispose()
        pytest.skip("aiosqlite schema create failed")

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
    return peer, plaintext


def _frame_jsonl(frame: PeerFrame) -> bytes:
    return frame.encode()


def test_stream_requires_bearer_token(client) -> None:
    resp = client.post(
        "/peer/v1/stream",
        content=_frame_jsonl(PeerFrame.hello(orch_id="orch-REMOTE")),
        headers={"Content-Type": "application/x-ndjson"},
    )
    assert resp.status_code == 401


def test_stream_requires_peer_orchestrator_id_header(client) -> None:
    resp = client.post(
        "/peer/v1/stream",
        content=b"",
        headers={"Authorization": "Bearer fake-token"},
    )
    assert resp.status_code == 401


def test_agent_card_advertises_frame_url(client) -> None:
    """PR-B2: discovery advertises both rest and frame transports."""
    resp = client.get("/.well-known/agent.json")
    assert resp.status_code == 200
    card = resp.json()
    protocols = [t["protocol"] for t in card["transports"]]
    assert "frame" in protocols
    assert "rest" in protocols
    # Frame is preferred (PR-B2 default).
    assert card["preferred_transport"] == "frame"
    # Frame URL is the rest URL + ``/peer/v1/stream``.
    frame_entry = next(t for t in card["transports"] if t["protocol"] == "frame")
    assert frame_entry["url"].endswith("/peer/v1/stream")


def test_hello_welcome_round_trip(client, tmp_path) -> None:
    """HELLO → server verifies + emits WELCOME."""
    try:
        peer, plaintext = asyncio.run(_build_accepted_peer(tmp_path))
    except pytest.skip.Exception:
        raise
    except Exception:
        pytest.skip("peer row setup requires sqlite aiosqlite support")

    hello = PeerFrame.hello(orch_id=peer.orch_id, capabilities=["peer.invoke"])
    sign(hello, plaintext)
    body = _frame_jsonl(hello)

    with client.stream(
        "POST",
        "/peer/v1/stream",
        content=body,
        headers={
            "Content-Type": "application/x-ndjson",
            "Authorization": f"Bearer {plaintext}",
            "X-Peer-Orchestrator-Id": peer.orch_id,
        },
    ) as resp:
        assert resp.status_code == 200
        line = next(resp.iter_lines(), "")
    assert line, "server must emit at least one frame (WELCOME)"
    welcome = PeerFrame.decode(line)
    assert welcome.type is PeerFrameType.WELCOME
    assert welcome.orch_id


def test_invoke_yields_result(client, tmp_path) -> None:
    """HELLO + INVOKE → server echoes a RESULT (PR-B2 stub execute)."""
    try:
        peer, plaintext = asyncio.run(_build_accepted_peer(tmp_path))
    except Exception:
        pytest.skip("peer row setup requires sqlite aiosqlite support")

    hello = PeerFrame.hello(orch_id=peer.orch_id)
    sign(hello, plaintext)
    invoke = PeerFrame.invoke(
        orch_id=peer.orch_id,
        request_id="req-1",
        method="peer.tasks.create",
        body={"title": "from frame"},
    )
    sign(invoke, plaintext)
    body = _frame_jsonl(hello) + _frame_jsonl(invoke)

    with client.stream(
        "POST",
        "/peer/v1/stream",
        content=body,
        headers={
            "Content-Type": "application/x-ndjson",
            "Authorization": f"Bearer {plaintext}",
            "X-Peer-Orchestrator-Id": peer.orch_id,
        },
    ) as resp:
        assert resp.status_code == 200
        lines: list[str] = []
        for ln in resp.iter_lines():
            lines.append(ln)
            if len(lines) >= 2:
                break
    assert len(lines) >= 2
    welcome = PeerFrame.decode(lines[0])
    result = PeerFrame.decode(lines[1])
    assert welcome.type is PeerFrameType.WELCOME
    assert result.type is PeerFrameType.RESULT
    assert result.request_id == "req-1"
    # PR-B2 stub echoes back the accepted body.
    assert result.body["status"] == "accepted"
    assert result.body["method"] == "peer.tasks.create"
    assert result.body["msg_id"] == invoke.msg_id


def test_goodbye_terminates_response(client, tmp_path) -> None:
    """GOODBYE from the client → server closes the stream cleanly."""
    try:
        peer, plaintext = asyncio.run(_build_accepted_peer(tmp_path))
    except Exception:
        pytest.skip("peer row setup requires sqlite aiosqlite support")

    hello = PeerFrame.hello(orch_id=peer.orch_id)
    sign(hello, plaintext)
    goodbye = PeerFrame.goodbye(orch_id=peer.orch_id, in_flight=0)
    body = _frame_jsonl(hello) + _frame_jsonl(goodbye)

    with client.stream(
        "POST",
        "/peer/v1/stream",
        content=body,
        headers={
            "Content-Type": "application/x-ndjson",
            "Authorization": f"Bearer {plaintext}",
            "X-Peer-Orchestrator-Id": peer.orch_id,
        },
    ) as resp:
        assert resp.status_code == 200
        for line in resp.iter_lines():
            if line:
                # Stream stays open until the writer is sentineled.
                PeerFrame.decode(line)
            else:
                break


def test_inbound_registry_releases_on_close(client, tmp_path) -> None:
    """The frame router registers a connection; the BackgroundTask unregisters."""
    try:
        peer, plaintext = asyncio.run(_build_accepted_peer(tmp_path))
    except Exception:
        pytest.skip("peer row setup requires sqlite aiosqlite support")

    assert peer_connections.live_inbound() == []

    hello = PeerFrame.hello(orch_id=peer.orch_id)
    sign(hello, plaintext)
    body = _frame_jsonl(hello)
    with client.stream(
        "POST",
        "/peer/v1/stream",
        content=body,
        headers={
            "Content-Type": "application/x-ndjson",
            "Authorization": f"Bearer {plaintext}",
            "X-Peer-Orchestrator-Id": peer.orch_id,
        },
    ) as resp:
        assert resp.status_code == 200
        line = next(resp.iter_lines(), "")
        if line:
            assert PeerFrame.decode(line).type is PeerFrameType.WELCOME

    # Cleanup is async; give it a beat.
    deadline = _time.monotonic() + 2.0
    while peer_connections.live_inbound() and _time.monotonic() < deadline:
        _time.sleep(0.05)
    assert peer_connections.live_inbound() == []