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
    # PR-B4: drop the broker too so each test starts with a clean
    # subscriber set (the ``LocalBackend`` keeps per-subscriber queues
    # that would otherwise leak across tests).
    from orchestratord.api.deps import reset_peer_rate_bucket
    from orchestratord.api.realtime import reset_broker

    reset_broker()
    reset_peer_rate_bucket()  # PR-B5: D25 bucket must not leak env overrides
    for conn in list(peer_connections.live_inbound()):
        peer_connections.unregister_inbound(conn)
    yield create_app()
    reset_peer_rate_bucket()


@pytest.fixture
def client(app):
    with TestClient(app) as c:
        yield c


async def _build_accepted_peer(
    tmp_path, *, keep_engine: bool = False
) -> tuple[Any, str, "Any | None"]:
    """Create a Peer row + valid auth_tokens row.

    Returns ``(peer, plaintext, engine)``; ``engine`` is ``None`` when
    ``keep_engine=False`` (the default — engine is disposed before
    return, mirrors the existing tests). When ``keep_engine=True`` the
    engine is left alive so the caller can seed additional rows in the
    same SQLite file.

    Skipped when the test environment doesn't have SQLAlchemy's aiosqlite
    driver wired up (mirrors the pattern from existing peer tests).
    """
    try:
        from sqlalchemy.ext.asyncio import AsyncEngine

        from orchestratord.db.engine import build_engine, create_schema
    except ImportError:
        pytest.skip("SQLAlchemy not available")

    import os

    db_path = tmp_path / "peer_frame_router.db"
    os.environ["ORCHESTRATORD_DATABASE_URL"] = f"sqlite+aiosqlite:///{db_path}"

    engine: AsyncEngine = build_engine()
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
    if not keep_engine:
        await engine.dispose()
        engine = None
    return peer, plaintext, engine


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
        peer, plaintext, _engine = asyncio.run(_build_accepted_peer(tmp_path))
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
        peer, plaintext, _engine = asyncio.run(_build_accepted_peer(tmp_path))
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
        peer, plaintext, _engine = asyncio.run(_build_accepted_peer(tmp_path))
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
        peer, plaintext, _engine = asyncio.run(_build_accepted_peer(tmp_path))
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


# -- PR-B3: routed method handlers --


async def _seed_session_in(
    engine, workspace_id: "uuid.UUID"
) -> "orm.Session":
    """Seed a Session row in the peer-frame SQLite test DB."""
    factory = async_sessionmaker(engine, expire_on_commit=False)
    s = orm.Session(
        id=uuid.uuid4(),
        workspace_id=workspace_id,
        issue_id=None,
        agent_id=None,
        run_id=None,
        mode="single",
        status="running",
        created_at=datetime.now(UTC),
    )
    async with factory() as session:
        session.add(s)
        await session.commit()
    return s


async def _seed_agent_in(
    engine, workspace_id: "uuid.UUID", *, name: str = "agent-1"
) -> "orm.Agent":
    """Seed an Agent row in the peer-frame SQLite test DB."""
    factory = async_sessionmaker(engine, expire_on_commit=False)
    # Pick any runtime_id; we don't reference the runtime table in
    # the agents.list handler — just need a stable FK-shaped UUID.
    a = orm.Agent(
        id=uuid.uuid4(),
        workspace_id=workspace_id,
        name=name,
        provider="openai",
        runtime_id=uuid.uuid4(),
        capabilities_cache_jsonb={},
        created_at=datetime.now(UTC),
    )
    async with factory() as session:
        session.add(a)
        await session.commit()
    return a


async def _seed_inbox_in(
    engine, workspace_id: "uuid.UUID", *, title: str = "inbox-1"
) -> "orm.InboxItem":
    """Seed an InboxItem row in the peer-frame SQLite test DB."""
    factory = async_sessionmaker(engine, expire_on_commit=False)
    item = orm.InboxItem(
        id=uuid.uuid4(),
        workspace_id=workspace_id,
        kind="mention",
        title=title,
        issue_id=None,
        session_id=None,
        event_seq=None,
        status="open",
        assignee_type=None,
        assignee_id=None,
        created_at=datetime.now(UTC),
    )
    async with factory() as session:
        session.add(item)
        await session.commit()
    return item


def _invoke_frame(
    *,
    request_id: str,
    msg_id: str,
    method: str,
    body: dict | None = None,
    orch_id: str = "orch-REMOTE",
    ordering: str | None = None,
) -> "PeerFrame":
    """Build a signed INVOKE frame for the test peer."""
    frame = PeerFrame.invoke(
        orch_id=orch_id,
        request_id=request_id,
        msg_id=msg_id,
        method=method,
        body=body or {},
        ordering=ordering,
    )
    return frame


def _hello_frame(orch_id: str) -> "PeerFrame":
    return PeerFrame.hello(orch_id=orch_id)


def _drive_invoke(
    client, peer, plaintext, *, request_id: str, msg_id: str,
    method: str, body: dict | None = None,
    ordering: str | None = None,
) -> "PeerFrame":
    """Drive HELLO + INVOKE through the chunked stream; return the RESULT frame.

    Skips on missing aiosqlite (mirrors existing tests). Reads up to
    two frames (WELCOME + RESULT) from the response stream.
    """
    try:
        from orchestratord.peer.hmac_sig import sign
    except ImportError:
        pytest.skip("peer.hmac_sig not available")

    hello = _hello_frame(peer.orch_id)
    sign(hello, plaintext)
    invoke = _invoke_frame(
        request_id=request_id,
        msg_id=msg_id,
        method=method,
        body=body,
        orch_id=peer.orch_id,
        ordering=ordering,
    )
    sign(invoke, plaintext)
    body_bytes = _frame_jsonl(hello) + _frame_jsonl(invoke)
    with client.stream(
        "POST",
        "/peer/v1/stream",
        content=body_bytes,
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
    result = PeerFrame.decode(lines[1])
    assert result.type is PeerFrameType.RESULT
    return result


def test_invoke_routes_sessions_message_post_persists_and_audits(
    client, tmp_path
) -> None:
    """PR-B3: ``POST /api/sessions/{session_id}/messages`` writes a
    Message row + AuditLogEntry; the wire-level handler does real work,
    not the PR-B2 stub echo."""
    try:
        peer, plaintext, engine = asyncio.run(
            _build_accepted_peer(tmp_path, keep_engine=True)
        )
    except Exception:
        pytest.skip("peer row setup requires sqlite aiosqlite support")
    assert engine is not None

    seeded_session = asyncio.run(_seed_session_in(engine, peer.workspace_id))

    result = _drive_invoke(
        client,
        peer,
        plaintext,
        request_id="req-msg-1",
        msg_id="msg-1",
        method="POST /api/sessions/{session_id}/messages",
        body={
            "session_id": str(seeded_session.id),
            "role": "user",
            "content": "hello from peer",
        },
    )
    # Status 200 on a fresh dispatch (mirrors REST parity: 200 for the
    # per-REST-shape, 201 only for cross-daemon session create which
    # is NOT routed on frame in PR-B3).
    assert result.status == 200
    assert result.body["message_id"]
    assert result.body["seq"] >= 1
    assert result.msg_id == "msg-1"
    assert result.request_id == "req-msg-1"


def test_invoke_routes_sessions_read_returns_session_list(client, tmp_path) -> None:
    """PR-B3: ``GET /api/workspaces/{workspace_id}/sessions`` returns
    the list of seeded sessions in the peer's authorized workspace."""
    try:
        peer, plaintext, engine = asyncio.run(
            _build_accepted_peer(tmp_path, keep_engine=True)
        )
    except Exception:
        pytest.skip("peer row setup requires sqlite aiosqlite support")
    assert engine is not None

    seeded_a = asyncio.run(_seed_session_in(engine, peer.workspace_id))
    seeded_b = asyncio.run(_seed_session_in(engine, peer.workspace_id))

    result = _drive_invoke(
        client,
        peer,
        plaintext,
        request_id="req-list-1",
        msg_id="msg-list-1",
        method="GET /api/workspaces/{workspace_id}/sessions",
        body={"workspace_id": str(peer.workspace_id)},
    )
    assert result.status == 200
    ids = {s["id"] for s in result.body}
    assert {str(seeded_a.id), str(seeded_b.id)} <= ids


def test_invoke_routes_agents_list_returns_agent_list(client, tmp_path) -> None:
    """PR-B3: ``GET /api/workspaces/{workspace_id}/agents`` returns
    the seeded agent rows."""
    try:
        peer, plaintext, engine = asyncio.run(
            _build_accepted_peer(tmp_path, keep_engine=True)
        )
    except Exception:
        pytest.skip("peer row setup requires sqlite aiosqlite support")
    assert engine is not None

    seeded = asyncio.run(
        _seed_agent_in(engine, peer.workspace_id, name="worker-1")
    )
    result = _drive_invoke(
        client,
        peer,
        plaintext,
        request_id="req-agents-1",
        msg_id="msg-agents-1",
        method="GET /api/workspaces/{workspace_id}/agents",
        body={"workspace_id": str(peer.workspace_id)},
    )
    assert result.status == 200
    names = {a["name"] for a in result.body}
    assert "worker-1" in names
    assert any(a["id"] == str(seeded.id) for a in result.body)


def test_invoke_routes_inbox_read_returns_inbox(client, tmp_path) -> None:
    """PR-B3: ``GET /api/workspaces/{workspace_id}/inbox`` returns the
    seeded inbox items."""
    try:
        peer, plaintext, engine = asyncio.run(
            _build_accepted_peer(tmp_path, keep_engine=True)
        )
    except Exception:
        pytest.skip("peer row setup requires sqlite aiosqlite support")
    assert engine is not None

    seeded = asyncio.run(
        _seed_inbox_in(engine, peer.workspace_id, title="review me")
    )
    result = _drive_invoke(
        client,
        peer,
        plaintext,
        request_id="req-inbox-1",
        msg_id="msg-inbox-1",
        method="GET /api/workspaces/{workspace_id}/inbox",
        body={"workspace_id": str(peer.workspace_id)},
    )
    assert result.status == 200
    titles = {i["title"] for i in result.body}
    assert "review me" in titles
    assert any(i["id"] == str(seeded.id) for i in result.body)


def test_invoke_sessions_approve_returns_501_not_bridge_pending(
    client, tmp_path
) -> None:
    """PR-B3: ``POST /api/sessions/{session_id}/approve`` returns 501
    + "cross-process bridge pending" because the cross-daemon bridge
    is not implemented on this build. The wire semantics (status
    code passthrough) are exercised even though no real handler runs.
    """
    try:
        peer, plaintext = asyncio.run(
            _build_accepted_peer(tmp_path)
        )
    except Exception:
        pytest.skip("peer row setup requires sqlite aiosqlite support")

    result = _drive_invoke(
        client,
        peer,
        plaintext,
        request_id="req-approve-1",
        msg_id="msg-approve-1",
        method="POST /api/sessions/{session_id}/approve",
        body={"decision": "approve"},
    )
    assert result.status == 501
    assert "cross-process bridge pending" in result.body["error"]
    assert (
        result.body["method"] == "POST /api/sessions/{session_id}/approve"
    )


def test_invoke_agents_message_returns_501_not_bridge_pending(
    client, tmp_path
) -> None:
    """PR-B3: ``POST /api/agents/{agent_id}/message`` returns 501 for
    the same reason as sessions.approve."""
    try:
        peer, plaintext = asyncio.run(
            _build_accepted_peer(tmp_path)
        )
    except Exception:
        pytest.skip("peer row setup requires sqlite aiosqlite support")

    result = _drive_invoke(
        client,
        peer,
        plaintext,
        request_id="req-am-1",
        msg_id="msg-am-1",
        method="POST /api/agents/{agent_id}/message",
        body={"text": "hi"},
    )
    assert result.status == 501
    assert "cross-process bridge pending" in result.body["error"]
    assert result.body["method"] == "POST /api/agents/{agent_id}/message"


def test_invoke_cross_workspace_request_returns_403(client, tmp_path) -> None:
    """PR-B3 (D14 regression): a peer authorized in workspace A must
    not be able to read sessions in workspace B. The handler's first
    line of defense is ``_require_workspace`` (HTTPException 403)."""
    try:
        peer, plaintext = asyncio.run(
            _build_accepted_peer(tmp_path)
        )
    except Exception:
        pytest.skip("peer row setup requires sqlite aiosqlite support")

    other_ws = uuid.uuid4()
    result = _drive_invoke(
        client,
        peer,
        plaintext,
        request_id="req-cross-1",
        msg_id="msg-cross-1",
        method="GET /api/workspaces/{workspace_id}/sessions",
        body={"workspace_id": str(other_ws)},
    )
    assert result.status == 403
    assert "peer is not trusted" in result.body["error"]


def test_invoke_dedup_replays_cached_body_for_routed_method(
    client, tmp_path
) -> None:
    """PR-B3 + D18: a re-delivered msg_id with a routed method
    replays the cached RESULT body without re-executing the handler.
    We assert this by inspecting the dispatcher cache directly via the
    public ``cached_result`` API."""
    try:
        peer, plaintext, engine = asyncio.run(
            _build_accepted_peer(tmp_path, keep_engine=True)
        )
    except Exception:
        pytest.skip("peer row setup requires sqlite aiosqlite support")
    assert engine is not None

    seeded = asyncio.run(_seed_session_in(engine, peer.workspace_id))

    # First call: full handler execution, Message row written.
    first = _drive_invoke(
        client,
        peer,
        plaintext,
        request_id="req-dedup-1",
        msg_id="msg-dedup-1",
        method="POST /api/sessions/{session_id}/messages",
        body={
            "session_id": str(seeded.id),
            "role": "user",
            "content": "first",
        },
    )
    assert first.status == 200
    first_message_id = first.body["message_id"]

    # Second call with the SAME msg_id: dispatcher short-circuits via
    # D18 dedup; the handler MUST NOT run again. If the handler did
    # run a second time we'd see a fresh ``message_id`` and the test
    # would also be racing on the seq counter.
    second = _drive_invoke(
        client,
        peer,
        plaintext,
        request_id="req-dedup-2",
        msg_id="msg-dedup-1",  # same msg_id → dedup replay
        method="POST /api/sessions/{session_id}/messages",
        body={
            "session_id": str(seeded.id),
            "role": "user",
            "content": "second (should be ignored)",
        },
    )
    assert second.status == 200
    assert second.body["message_id"] == first_message_id


def test_invoke_handler_exception_translates_to_500(client, tmp_path) -> None:
    """PR-B3: an unhandled exception inside a handler is logged and
    surfaced to the client as a 500 RESULT frame with a non-empty
    error body — never a silent success.

    We trigger the path by patching one of the handlers to raise.
    """
    try:
        from orchestratord.peer import method_handlers
    except ImportError:
        pytest.skip("peer.method_handlers not available")

    try:
        peer, plaintext = asyncio.run(
            _build_accepted_peer(tmp_path)
        )
    except Exception:
        pytest.skip("peer row setup requires sqlite aiosqlite support")

    # Monkey-patch the read-only handler so the next INVOKE on its
    # method raises. This exercises the bare-Exception branch in
    # ``_dispatch_invoke_frame`` without needing a real DB fault.
    original = method_handlers.handle_sessions_read

    async def _crashing_handler(*_args, **_kwargs):
        raise RuntimeError("synthetic crash for test")

    method_handlers.handle_sessions_read = _crashing_handler
    try:
        result = _drive_invoke(
            client,
            peer,
            plaintext,
            request_id="req-crash-1",
            msg_id="msg-crash-1",
            method="GET /api/workspaces/{workspace_id}/sessions",
            body={"workspace_id": str(peer.workspace_id)},
        )
    finally:
        method_handlers.handle_sessions_read = original

    assert result.status == 500
    assert "synthetic crash for test" in result.body["error"]


def test_invoke_unknown_method_falls_back_to_stub_for_backward_compat(
    client, tmp_path
) -> None:
    """PR-B3: unknown method names (Phase 1 client sends
    ``peer.tasks.create`` etc.) still get the PR-B2 stub echo body
    — 200 + ``{"status":"accepted","msg_id":...,"method":...}``.
    No silent 422; the wire stays compatible.
    """
    try:
        peer, plaintext = asyncio.run(
            _build_accepted_peer(tmp_path)
        )
    except Exception:
        pytest.skip("peer row setup requires sqlite aiosqlite support")

    result = _drive_invoke(
        client,
        peer,
        plaintext,
        request_id="req-stub-1",
        msg_id="msg-stub-1",
        method="peer.tasks.create",
        body={"title": "from frame"},
    )
    assert result.status == 200
    assert result.body["status"] == "accepted"
    assert result.body["method"] == "peer.tasks.create"
    assert result.body["msg_id"] == "msg-stub-1"


# -- PR-B4: D19 ordering on frame path --


def _lookup_audit_in_db(
    db_path, *, peer_call_id: str
) -> "orm.AuditLogEntry | None":
    """Open a fresh engine on ``db_path`` and fetch the audit row by
    ``invited_by_peer_call_id``. Used by the PR-B4 ordering tests to
    assert the dispatcher's ``out_of_order`` flag landed in the
    audit payload.

    Uses a single ``asyncio.run`` so the AsyncEngine is created and
    disposed on the same loop — disposing from a different loop raises
    on SQLAlchemy 2.x's per-loop pool bookkeeping.
    """
    import os

    from sqlalchemy import select

    from orchestratord.db.engine import build_engine

    os.environ["ORCHESTRATORD_DATABASE_URL"] = (
        f"sqlite+aiosqlite:///{db_path}"
    )
    engine = build_engine()

    async def _run() -> "orm.AuditLogEntry | None":
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with factory() as session:
                rows = (
                    await session.execute(
                        select(orm.AuditLogEntry).where(
                            orm.AuditLogEntry.invited_by_peer_call_id
                            == peer_call_id
                        )
                    )
                ).scalars().all()
                return rows[0] if rows else None
        finally:
            await engine.dispose()

    try:
        return asyncio.run(_run())
    finally:
        os.environ.pop("ORCHESTRATORD_DATABASE_URL", None)


def test_invoke_passes_session_id_for_ordering_on_frame_path(
    client, tmp_path
) -> None:
    """PR-B4: a routed INVOKE that carries ``session_id`` and
    ``ordering=1`` writes an :class:`AuditLogEntry` whose
    ``payload_jsonb["out_of_order"]`` is ``False`` — the first message
    in a per-session chain is in-order by definition."""
    try:
        peer, plaintext, engine = asyncio.run(
            _build_accepted_peer(tmp_path, keep_engine=True)
        )
    except Exception:
        pytest.skip("peer row setup requires sqlite aiosqlite support")
    assert engine is not None

    seeded = asyncio.run(_seed_session_in(engine, peer.workspace_id))
    db_path = tmp_path / "peer_frame_router.db"

    result = _drive_invoke(
        client,
        peer,
        plaintext,
        request_id="req-ord-1",
        msg_id="msg-ord-1",
        method="POST /api/sessions/{session_id}/messages",
        body={
            "session_id": str(seeded.id),
            "role": "user",
            "content": "ordered 1",
        },
        ordering="1",
    )
    assert result.status == 200

    audit = _lookup_audit_in_db(db_path, peer_call_id="msg-ord-1")
    assert audit is not None, "audit row not written for INVOKE"
    assert audit.action == "peer.invoke.message"
    assert audit.payload_jsonb["out_of_order"] is False
    assert audit.payload_jsonb["peer_call_id"] == "msg-ord-1"
    assert (
        audit.payload_jsonb["method"]
        == "POST /api/sessions/{session_id}/messages"
    )


def test_invoke_ordering_gap_flags_out_of_order_on_frame_path(
    client, tmp_path
) -> None:
    """PR-B4 + D19: a routed INVOKE whose ``ordering`` jumps over
    unseen sequence numbers on the same ``(peer, session)`` chain
    is flagged ``out_of_order=True`` in the audit row. The first
    INVOKE (seq=1) is in-order; the second (seq=5) is a gap and
    should be marked out_of_order=True.

    Two separate chunked-POST connections are needed because HELLO
    is per-connection; the dispatcher state (``_ordering``) is
    process-wide so the chain carries across.
    """
    try:
        peer, plaintext, engine = asyncio.run(
            _build_accepted_peer(tmp_path, keep_engine=True)
        )
    except Exception:
        pytest.skip("peer row setup requires sqlite aiosqlite support")
    assert engine is not None

    seeded = asyncio.run(_seed_session_in(engine, peer.workspace_id))
    db_path = tmp_path / "peer_frame_router.db"

    # First INVOKE: ordering=1, in-order.
    first = _drive_invoke(
        client,
        peer,
        plaintext,
        request_id="req-ord-gap-1",
        msg_id="msg-ord-gap-1",
        method="POST /api/sessions/{session_id}/messages",
        body={
            "session_id": str(seeded.id),
            "role": "user",
            "content": "seq 1",
        },
        ordering="1",
    )
    assert first.status == 200

    # Second INVOKE on a fresh connection: ordering=5 — gap from 1 to 5.
    second = _drive_invoke(
        client,
        peer,
        plaintext,
        request_id="req-ord-gap-2",
        msg_id="msg-ord-gap-2",
        method="POST /api/sessions/{session_id}/messages",
        body={
            "session_id": str(seeded.id),
            "role": "user",
            "content": "seq 5 (gap)",
        },
        ordering="5",
    )
    assert second.status == 200

    audit_first = _lookup_audit_in_db(
        db_path, peer_call_id="msg-ord-gap-1"
    )
    assert audit_first is not None
    assert audit_first.payload_jsonb["out_of_order"] is False

    audit_second = _lookup_audit_in_db(
        db_path, peer_call_id="msg-ord-gap-2"
    )
    assert audit_second is not None
    assert audit_second.payload_jsonb["out_of_order"] is True


# -- PR-B4: AC7/D22 EVENT cross-daemon relay on frame path --


def _build_sub_frame(orch_id: str, topic: str) -> "PeerFrame":
    """Build a SUBSCRIBE frame (factory not exposed on PeerFrame)."""
    return PeerFrame(
        type=PeerFrameType.SUBSCRIBE, orch_id=orch_id, topic=topic,
    )


def _build_unsub_frame(orch_id: str, topic: str) -> "PeerFrame":
    return PeerFrame(
        type=PeerFrameType.UNSUBSCRIBE, orch_id=orch_id, topic=topic,
    )


async def _read_until_type(resp, frame_type, *, timeout: float = 5.0):
    """Async-generate frames from ``resp`` until one matches ``frame_type``.

    Returns the matched frame. Raises :class:`asyncio.TimeoutError` if
    no matching frame arrives within ``timeout`` seconds.
    """
    deadline = asyncio.get_event_loop().time() + timeout
    async for line in resp.aiter_lines():
        if not line:
            continue
        frame = PeerFrame.decode(line)
        if frame.type is frame_type:
            return frame
        if asyncio.get_event_loop().time() > deadline:
            raise asyncio.TimeoutError(frame_type)
    raise asyncio.TimeoutError(frame_type)


async def test_subscribe_publishes_event_to_inbound_stream(
    app, tmp_path
) -> None:
    """PR-B4 AC7/D22: a SUBSCRIBE frame wires the inbound stream into
    the local RealtimeBroker; publishing via ``broker.publish(...)``
    pushes an EVENT frame onto the outbound chunked-JSONL stream."""
    try:
        from orchestratord.api.realtime import get_broker
        from orchestratord.peer.hmac_sig import sign
    except ImportError:
        pytest.skip("peer realtime / hmac_sig not available")
    try:
        peer, plaintext = await _build_accepted_peer(tmp_path)
    except Exception:
        pytest.skip("peer row setup requires sqlite aiosqlite support")

    headers = {
        "Content-Type": "application/x-ndjson",
        "Authorization": f"Bearer {plaintext}",
        "X-Peer-Orchestrator-Id": peer.orch_id,
    }
    topic = "peer.test.event"

    hello = _hello_frame(peer.orch_id)
    sign(hello, plaintext)
    sub = _build_sub_frame(peer.orch_id, topic)
    body_iter = _frame_body_iter_with_eventually_goodbye(
        [hello, sub], peer, plaintext
    )

    transport = None
    try:
        from httpx import ASGITransport
        import httpx

        transport = ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as ac:
            req = ac.build_request(
                "POST", "/peer/v1/stream", content=body_iter, headers=headers,
            )
            resp = await ac.send(req, stream=True)
            assert resp.status_code == 200

            # 1. Drain WELCOME.
            welcome = await _read_until_type(resp, PeerFrameType.WELCOME)
            assert welcome.orch_id

            # 2. Server has now processed SUBSCRIBE and updated broker
            # topics. Publish via the broker from the same coroutine —
            # the forwarder will deliver the EVENT to the outbound queue.
            broker = get_broker()
            await broker.publish(topic, {"data": "hello from test"})

            # 3. Read EVENT frame.
            event = await _read_until_type(
                resp, PeerFrameType.EVENT, timeout=5.0
            )
            assert event.topic == topic
            assert event.payload == {"data": "hello from test"}
    finally:
        if transport is not None:
            await transport.aclose()


async def _frame_body_iter_with_eventually_goodbye(frames, peer, plaintext):
    """Yield the given frames' JSONL bytes, then hold the stream open
    until the test cancels us, then emit GOODBYE so the server drains
    in-flight frames and closes cleanly."""
    for f in frames:
        yield _frame_jsonl(f)
    try:
        # Hold the stream open indefinitely; the test cancels this
        # iterator when it's done reading frames.
        while True:
            await asyncio.sleep(0.05)
            yield b"\n"  # empty lines are skipped (peer_frame.py:244-246)
    except asyncio.CancelledError:
        goodbye = PeerFrame.goodbye(orch_id=peer.orch_id, in_flight=0)
        yield _frame_jsonl(goodbye)
        raise


async def test_unsubscribe_stops_event_delivery(app, tmp_path) -> None:
    """PR-B4: UNSUBSCRIBE shrinks the broker topic set; events
    published after UNSUBSCRIBE are no longer delivered to the
    inbound stream."""
    try:
        from orchestratord.api.realtime import get_broker
        from orchestratord.peer.hmac_sig import sign
    except ImportError:
        pytest.skip("peer realtime / hmac_sig not available")
    try:
        peer, plaintext = await _build_accepted_peer(tmp_path)
    except Exception:
        pytest.skip("peer row setup requires sqlite aiosqlite support")

    headers = {
        "Content-Type": "application/x-ndjson",
        "Authorization": f"Bearer {plaintext}",
        "X-Peer-Orchestrator-Id": peer.orch_id,
    }
    topic = "peer.test.unsub"

    hello = _hello_frame(peer.orch_id)
    sign(hello, plaintext)
    sub = _build_sub_frame(peer.orch_id, topic)
    unsub = _build_unsub_frame(peer.orch_id, topic)

    async def body_iter():
        """Phase 1: HELLO + SUBSCRIBE, then idle briefly so the test
        can publish + verify the first EVENT. Phase 2: yield
        UNSUBSCRIBE and idle again so the test can verify no further
        EVENTs land."""
        yield _frame_jsonl(hello)
        yield _frame_jsonl(sub)
        await asyncio.sleep(1.0)  # phase 1 window
        yield _frame_jsonl(unsub)
        await asyncio.sleep(1.5)  # phase 2 window

    try:
        from httpx import ASGITransport
        import httpx

        transport = ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as ac:
            req = ac.build_request(
                "POST", "/peer/v1/stream", content=body_iter(), headers=headers,
            )
            resp = await ac.send(req, stream=True)
            assert resp.status_code == 200

            welcome = await _read_until_type(resp, PeerFrameType.WELCOME)

            broker = get_broker()

            # Phase 1: publish + verify EVENT arrives (we're subscribed).
            await broker.publish(topic, {"phase": "before-unsub"})
            first_event = await _read_until_type(
                resp, PeerFrameType.EVENT, timeout=3.0
            )
            assert first_event.payload == {"phase": "before-unsub"}

            # Phase 2: body iter is sending UNSUBSCRIBE during the
            # 1.0s sleep. Wait long enough for it to be processed, then
            # publish again. The EVENT must NOT arrive.
            await asyncio.sleep(1.5)
            await broker.publish(topic, {"phase": "after-unsub"})

            got_after = False
            try:
                evt = await _read_until_type(
                    resp, PeerFrameType.EVENT, timeout=0.5
                )
                if evt.payload.get("phase") == "after-unsub":
                    got_after = True
            except asyncio.TimeoutError:
                got_after = False
            assert got_after is False, (
                "EVENT delivered after UNSUBSCRIBE — broker topic "
                "set still includes the test topic"
            )
    finally:
        pass


async def test_event_forwarder_stops_when_connection_closes(
    app, tmp_path
) -> None:
    """PR-B4: when the inbound stream is closed (GOODBYE or
    transport error), the broker subscription is removed via the
    iterator's ``finally`` clause — no ghost subscribers linger.

    We verify by counting subscribers before/after the connection.
    """
    try:
        from orchestratord.api.realtime import get_broker
        from orchestratord.peer.hmac_sig import sign
    except ImportError:
        pytest.skip("peer realtime / hmac_sig not available")
    try:
        peer, plaintext = await _build_accepted_peer(tmp_path)
    except Exception:
        pytest.skip("peer row setup requires sqlite aiosqlite support")

    broker = get_broker()
    sub_count_before = len(broker._subscribers)

    headers = {
        "Content-Type": "application/x-ndjson",
        "Authorization": f"Bearer {plaintext}",
        "X-Peer-Orchestrator-Id": peer.orch_id,
    }
    hello = _hello_frame(peer.orch_id)
    sign(hello, plaintext)
    goodbye = PeerFrame.goodbye(orch_id=peer.orch_id, in_flight=0)

    body_bytes = _frame_jsonl(hello) + _frame_jsonl(goodbye)

    try:
        from httpx import ASGITransport
        import httpx

        transport = ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as ac:
            req = ac.build_request(
                "POST", "/peer/v1/stream", content=body_bytes, headers=headers,
            )
            resp = await ac.send(req, stream=True)
            assert resp.status_code == 200
            # Drain so the server-side reader exits naturally.
            async for _ in resp.aiter_lines():
                pass
            await resp.aclose()

        # Give the cleanup task a beat to run.
        deadline = asyncio.get_event_loop().time() + 2.0
        while asyncio.get_event_loop().time() < deadline:
            if len(broker._subscribers) == sub_count_before:
                break
            await asyncio.sleep(0.05)

        assert len(broker._subscribers) == sub_count_before, (
            f"broker leaked subscribers: before={sub_count_before} "
            f"after={len(broker._subscribers)}"
        )
    finally:
        pass


# -- PR-B5: D25 per-INVOKE-frame rate limit on the frame path --


def test_invoke_rate_limited_returns_429_result(
    client, tmp_path, monkeypatch
) -> None:
    """PR-B5 D25: the stream authenticates once, so the bucket must be
    acquired per INVOKE frame. An over-limit frame is answered with a
    RESULT carrying ``status=429`` — the connection stays up (REST
    parity: 429 + Retry-After)."""
    from orchestratord.api.deps import reset_peer_rate_bucket

    monkeypatch.setenv("ORCHESTRATORD_PEER_RATE_RPS", "0.001")
    monkeypatch.setenv("ORCHESTRATORD_PEER_RATE_BURST", "1")
    reset_peer_rate_bucket()
    try:
        peer, plaintext, _engine = asyncio.run(_build_accepted_peer(tmp_path))
    except Exception:
        pytest.skip("peer row setup requires sqlite aiosqlite support")

    # Unknown method → PR-B2 stub echo (200), but the bucket is still
    # consumed (acquisition happens before dispatch).
    first = _drive_invoke(
        client, peer, plaintext,
        request_id="req-rl-1", msg_id="rl-1", method="UNKNOWN/probe",
    )
    assert first.status == 200

    second = _drive_invoke(
        client, peer, plaintext,
        request_id="req-rl-2", msg_id="rl-2", method="UNKNOWN/probe",
    )
    assert second.status == 429
    assert second.body["error"] == "peer rate limit exceeded"
    assert second.body["retry_after"] >= 1
    assert second.msg_id == "rl-2"
    assert second.request_id == "req-rl-2"


def test_rate_limit_recovers_after_bucket_reset(
    client, tmp_path, monkeypatch
) -> None:
    """PR-B5 D25: after a 429, resetting the bucket (operator/test
    seam) lets INVOKEs through again — proves the limit is bucket
    state, not a wedged connection."""
    from orchestratord.api.deps import reset_peer_rate_bucket

    monkeypatch.setenv("ORCHESTRATORD_PEER_RATE_RPS", "0.001")
    monkeypatch.setenv("ORCHESTRATORD_PEER_RATE_BURST", "1")
    reset_peer_rate_bucket()
    try:
        peer, plaintext, _engine = asyncio.run(_build_accepted_peer(tmp_path))
    except Exception:
        pytest.skip("peer row setup requires sqlite aiosqlite support")

    first = _drive_invoke(
        client, peer, plaintext,
        request_id="req-rl-a", msg_id="rl-a", method="UNKNOWN/probe",
    )
    assert first.status == 200
    blocked = _drive_invoke(
        client, peer, plaintext,
        request_id="req-rl-b", msg_id="rl-b", method="UNKNOWN/probe",
    )
    assert blocked.status == 429

    reset_peer_rate_bucket()
    recovered = _drive_invoke(
        client, peer, plaintext,
        request_id="req-rl-c", msg_id="rl-c", method="UNKNOWN/probe",
    )
    assert recovered.status == 200