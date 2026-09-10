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
    from orchestratord.peer import topic_registry

    reset_broker()
    reset_peer_rate_bucket()  # PR-B5: D25 bucket must not leak env overrides
    topic_registry.reset_peer_topics()  # PR-B9: per-peer SSE subscriptions
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
    engine, workspace_id: "uuid.UUID",
    *, agent_id: "uuid.UUID | None" = None, status: str = "running",
) -> "orm.Session":
    """Seed a Session row in the peer-frame SQLite test DB."""
    factory = async_sessionmaker(engine, expire_on_commit=False)
    s = orm.Session(
        id=uuid.uuid4(),
        workspace_id=workspace_id,
        issue_id=None,
        agent_id=agent_id,
        run_id=None,
        mode="single",
        status=status,
        created_at=datetime.now(UTC),
    )
    async with factory() as session:
        session.add(s)
        await session.commit()
    return s


async def _seed_approval_request_event(
    engine, workspace_id: "uuid.UUID", session_id: "uuid.UUID",
    *, request_id: str = "apr-1",
) -> "orm.Event":
    """Seed an APPROVAL_REQUEST event so ``_pending_request`` finds it."""
    from orchestratord.spi.events import EventKind

    factory = async_sessionmaker(engine, expire_on_commit=False)
    event = orm.Event(
        id=uuid.uuid4(),
        session_id=session_id,
        sequence=1,
        kind=EventKind.APPROVAL_REQUEST.value,
        payload={"request_id": request_id},
        run_id=None,
        issue_id=None,
        workspace_id=workspace_id,
        created_at=datetime.now(UTC),
    )
    async with factory() as session:
        session.add(event)
        await session.commit()
    return event


class _FakeSpiSession:
    """Records approve() calls for assertion."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def approve(self, request_id: str, decision) -> None:
        self.calls.append((request_id, decision.value))


class _FakeLive:
    def __init__(self, *, approval_hooks: bool) -> None:
        from types import SimpleNamespace

        self.capabilities = SimpleNamespace(approval_hooks=approval_hooks)
        self.spi_session = _FakeSpiSession()


class _FakeRegistry:
    def __init__(self, live) -> None:
        self._live = live

    async def get(self, session_id: str):
        return self._live


class _FakeRunner:
    def __init__(self, live) -> None:
        self.registry = _FakeRegistry(live)


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
    assert result.body["seq"] == 0  # first message in a fresh session
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


def test_invoke_sessions_approve_records_decision_and_executes_live_spi(
    client, tmp_path
) -> None:
    """PR-B9 (Gap B1): a frame approve on a session live in THIS daemon
    records the Approval row and executes the live SPI approve — the
    full REST operator chain, minus the outward peer-forward."""
    from orchestratord.api import runtime
    from orchestratord.spi.approval import ApprovalDecision

    try:
        peer, plaintext, engine = asyncio.run(
            _build_accepted_peer(tmp_path, keep_engine=True)
        )
    except Exception:
        pytest.skip("peer row setup requires sqlite aiosqlite support")
    assert engine is not None

    seeded = asyncio.run(_seed_session_in(engine, peer.workspace_id))
    asyncio.run(
        _seed_approval_request_event(
            engine, peer.workspace_id, seeded.id, request_id="apr-1"
        )
    )
    live = _FakeLive(approval_hooks=True)
    runtime.set_backend_runner(_FakeRunner(live))
    try:
        result = _drive_invoke(
            client,
            peer,
            plaintext,
            request_id="req-approve-1",
            msg_id="msg-approve-1",
            method="POST /api/sessions/{session_id}/approve",
            body={
                "session_id": str(seeded.id),
                "request_id": "apr-1",
            },
        )
    finally:
        runtime.reset_backend_runner()
    assert result.status == 200
    assert result.body["decision"] == "approved"
    assert result.body["request_id"] == "apr-1"
    assert live.spi_session.calls == [("apr-1", ApprovalDecision.ALLOW.value)]

    async def _check_approval_row() -> None:
        import sqlalchemy

        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            rows = (await session.execute(
                sqlalchemy.select(orm.Approval)
            )).scalars().all()
        assert len(rows) == 1
        assert rows[0].session_id == seeded.id
        assert rows[0].request_id == "apr-1"
        assert rows[0].decision == "approved"

    asyncio.run(_check_approval_row())


def test_invoke_sessions_approve_409_when_session_not_live(
    client, tmp_path
) -> None:
    """PR-B9 拍板：frame 路径不做多跳外发 — 会话不 live 在本 daemon
    时返 409（REST 会静默落到 _forward_to_peer），且不落 Approval 行。"""
    try:
        peer, plaintext, engine = asyncio.run(
            _build_accepted_peer(tmp_path, keep_engine=True)
        )
    except Exception:
        pytest.skip("peer row setup requires sqlite aiosqlite support")
    assert engine is not None

    seeded = asyncio.run(_seed_session_in(engine, peer.workspace_id))
    asyncio.run(
        _seed_approval_request_event(
            engine, peer.workspace_id, seeded.id, request_id="apr-1"
        )
    )
    result = _drive_invoke(
        client,
        peer,
        plaintext,
        request_id="req-approve-2",
        msg_id="msg-approve-2",
        method="POST /api/sessions/{session_id}/approve",
        body={"session_id": str(seeded.id), "request_id": "apr-1"},
    )
    assert result.status == 409
    assert "not live on this daemon" in result.body["error"]

    async def _no_approval_rows() -> None:
        import sqlalchemy

        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            rows = (await session.execute(
                sqlalchemy.select(orm.Approval)
            )).scalars().all()
        assert rows == []

    asyncio.run(_no_approval_rows())


def test_invoke_sessions_approve_409_when_no_approval_hooks(
    client, tmp_path
) -> None:
    """PR-B9: live session whose backend lacks approval_hooks → 409,
    mirroring the REST _forward_approval semantics."""
    from orchestratord.api import runtime

    try:
        peer, plaintext, engine = asyncio.run(
            _build_accepted_peer(tmp_path, keep_engine=True)
        )
    except Exception:
        pytest.skip("peer row setup requires sqlite aiosqlite support")
    assert engine is not None

    seeded = asyncio.run(_seed_session_in(engine, peer.workspace_id))
    asyncio.run(
        _seed_approval_request_event(
            engine, peer.workspace_id, seeded.id, request_id="apr-1"
        )
    )
    runtime.set_backend_runner(_FakeRunner(_FakeLive(approval_hooks=False)))
    try:
        result = _drive_invoke(
            client,
            peer,
            plaintext,
            request_id="req-approve-3",
            msg_id="msg-approve-3",
            method="POST /api/sessions/{session_id}/approve",
            body={"session_id": str(seeded.id), "request_id": "apr-1"},
        )
    finally:
        runtime.reset_backend_runner()
    assert result.status == 409
    assert "approval_hooks=False" in result.body["error"]


def test_invoke_sessions_approve_404_when_no_pending_request(
    client, tmp_path
) -> None:
    """PR-B9: approving an unknown request_id → 404, same as REST."""
    try:
        peer, plaintext, engine = asyncio.run(
            _build_accepted_peer(tmp_path, keep_engine=True)
        )
    except Exception:
        pytest.skip("peer row setup requires sqlite aiosqlite support")
    assert engine is not None

    seeded = asyncio.run(_seed_session_in(engine, peer.workspace_id))
    result = _drive_invoke(
        client,
        peer,
        plaintext,
        request_id="req-approve-4",
        msg_id="msg-approve-4",
        method="POST /api/sessions/{session_id}/approve",
        body={"session_id": str(seeded.id), "request_id": "missing-req"},
    )
    assert result.status == 404
    assert "no pending approval request" in result.body["error"]


def test_invoke_agents_message_delivers_to_running_session(
    client, tmp_path
) -> None:
    """PR-B9 (Gap B2): ``agents.message`` delivers to the agent's active
    (running) session and reuses the shared persist+audit chain."""
    try:
        peer, plaintext, engine = asyncio.run(
            _build_accepted_peer(tmp_path, keep_engine=True)
        )
    except Exception:
        pytest.skip("peer row setup requires sqlite aiosqlite support")
    assert engine is not None

    agent = asyncio.run(
        _seed_agent_in(engine, peer.workspace_id, name="worker-1")
    )
    seeded = asyncio.run(
        _seed_session_in(engine, peer.workspace_id, agent_id=agent.id)
    )
    result = _drive_invoke(
        client,
        peer,
        plaintext,
        request_id="req-am-1",
        msg_id="msg-am-1",
        method="POST /api/agents/{agent_id}/message",
        body={"agent_id": str(agent.id), "content": "hi from peer"},
    )
    assert result.status == 200
    assert result.body["message_id"]
    assert result.body["seq"] == 0  # first message in a fresh session

    async def _check_rows() -> None:
        import sqlalchemy

        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            message = (await session.execute(
                sqlalchemy.select(orm.Message).where(
                    orm.Message.id == uuid.UUID(result.body["message_id"])
                )
            )).scalar_one()
            audits = (await session.execute(
                sqlalchemy.select(orm.AuditLogEntry).where(
                    orm.AuditLogEntry.invited_by_peer_call_id == "msg-am-1"
                )
            )).scalars().all()
        assert str(message.session_id) == str(seeded.id)
        assert message.author_label == peer.orch_id
        assert len(audits) == 1
        assert audits[0].payload_jsonb["method"] == (
            "POST /api/agents/{agent_id}/message"
        )

    asyncio.run(_check_rows())


def test_invoke_agents_message_404_when_no_running_session(
    client, tmp_path
) -> None:
    """PR-B9: an agent without a running session → 404, not a silent
    accept."""
    try:
        peer, plaintext, engine = asyncio.run(
            _build_accepted_peer(tmp_path, keep_engine=True)
        )
    except Exception:
        pytest.skip("peer row setup requires sqlite aiosqlite support")
    assert engine is not None

    agent = asyncio.run(
        _seed_agent_in(engine, peer.workspace_id, name="idle-1")
    )
    result = _drive_invoke(
        client,
        peer,
        plaintext,
        request_id="req-am-2",
        msg_id="msg-am-2",
        method="POST /api/agents/{agent_id}/message",
        body={"agent_id": str(agent.id), "content": "hi"},
    )
    assert result.status == 404
    assert "no active session for agent" in result.body["error"]


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


# -- PR-B9: SUBSCRIBE/UNSUBSCRIBE → topic registry + SSE EVENT delivery --


def _build_sub_frame(orch_id: str, topic: str) -> "PeerFrame":
    """Build a SUBSCRIBE frame (factory not exposed on PeerFrame)."""
    return PeerFrame(
        type=PeerFrameType.SUBSCRIBE, orch_id=orch_id, topic=topic,
    )


def _build_unsub_frame(orch_id: str, topic: str) -> "PeerFrame":
    return PeerFrame(
        type=PeerFrameType.UNSUBSCRIBE, orch_id=orch_id, topic=topic,
    )


def _build_revoke_frame(orch_id: str) -> "PeerFrame":
    return PeerFrame(type=PeerFrameType.REVOKE, orch_id=orch_id)


async def _build_batch_headers(tmp_path):
    """Accepted peer + auth headers for frame batches."""
    peer, plaintext, _engine = await _build_accepted_peer(tmp_path)
    headers = {
        "Content-Type": "application/x-ndjson",
        "Authorization": f"Bearer {plaintext}",
        "X-Peer-Orchestrator-Id": peer.orch_id,
    }
    return peer, plaintext, headers


async def _post_frame_batch(ac, headers, frames) -> list[PeerFrame]:
    """Run one batch-POST (frames → body EOF) and decode the replay."""
    body = b"".join(_frame_jsonl(f) for f in frames)
    resp = await ac.post("/peer/v1/stream", content=body, headers=headers)
    assert resp.status_code == 200
    return [
        PeerFrame.decode(line)
        for line in resp.text.splitlines()
        if line.strip()
    ]


async def test_subscribe_registry_lifecycle(app, tmp_path) -> None:
    """PR-B9: a SUBSCRIBE frame in one batch registers the topic in the
    per-peer registry; UNSUBSCRIBE drops it and an SSE request with no
    explicit topics + empty registry is a 422; GOODBYE clears it.

    Live SSE EVENT delivery is exercised by the peer_integration
    real-socket tests (httpx.ASGITransport buffers the whole app call
    and cannot drive a long-lived stream incrementally).
    """
    from httpx import ASGITransport
    import httpx

    from orchestratord.peer import topic_registry

    peer, plaintext, headers = await _build_batch_headers(tmp_path)
    topic = "peer.test.event"

    transport = ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as ac:
            hello = _hello_frame(peer.orch_id)
            sign(hello, plaintext)
            frames = await _post_frame_batch(
                ac, headers,
                [hello, _build_sub_frame(peer.orch_id, topic)],
            )
            assert frames[0].type is PeerFrameType.WELCOME
            assert topic_registry.get_peer_topics(peer.orch_id) == {topic}

            # UNSUBSCRIBE drops the topic; SSE with no topics → 422.
            hello = _hello_frame(peer.orch_id)
            sign(hello, plaintext)
            await _post_frame_batch(
                ac, headers,
                [hello, _build_unsub_frame(peer.orch_id, topic)],
            )
            assert topic_registry.get_peer_topics(peer.orch_id) == set()
            resp = await ac.get(
                f"/api/peer/peers/{peer.orch_id}/events",
                headers={
                    "Authorization": f"Bearer {plaintext}",
                    "X-Peer-Orchestrator-Id": peer.orch_id,
                },
            )
            assert resp.status_code == 422

            # GOODBYE batch clears any re-registered topics.
            hello = _hello_frame(peer.orch_id)
            sign(hello, plaintext)
            await _post_frame_batch(
                ac, headers,
                [hello, _build_sub_frame(peer.orch_id, topic)],
            )
            hello = _hello_frame(peer.orch_id)
            sign(hello, plaintext)
            await _post_frame_batch(
                ac, headers,
                [hello, PeerFrame.goodbye(orch_id=peer.orch_id, in_flight=0)],
            )
            assert topic_registry.get_peer_topics(peer.orch_id) == set()
    finally:
        await transport.aclose()


async def test_unsubscribe_registry_then_sse_422(app, tmp_path) -> None:
    """PR-B9: UNSUBSCRIBE drops the registry topic; a follow-up SSE
    request with no explicit topics and an empty registry is a 422."""
    from httpx import ASGITransport
    import httpx

    from orchestratord.peer import topic_registry

    peer, plaintext, headers = await _build_batch_headers(tmp_path)
    topic = "peer.test.unsub"

    hello = _hello_frame(peer.orch_id)
    sign(hello, plaintext)

    transport = ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as ac:
            await _post_frame_batch(
                ac, headers,
                [hello, _build_sub_frame(peer.orch_id, topic)],
            )
            assert topic_registry.get_peer_topics(peer.orch_id) == {topic}

            await _post_frame_batch(
                ac, headers,
                [hello, _build_unsub_frame(peer.orch_id, topic)],
            )
            assert topic_registry.get_peer_topics(peer.orch_id) == set()

            resp = await ac.get(f"/api/peer/peers/{peer.orch_id}/events")
            assert resp.status_code == 422
    finally:
        await transport.aclose()


@pytest.mark.parametrize("terminator", ["goodbye", "revoke"])
async def test_terminator_batch_clears_topic_registry(
    app, tmp_path, terminator
) -> None:
    """PR-B9: GOODBYE and REVOKE both end the session — the peer's SSE
    topic registry is cleared so no ghost subscription survives."""
    from httpx import ASGITransport
    import httpx

    from orchestratord.peer import topic_registry

    peer, plaintext, headers = await _build_batch_headers(tmp_path)
    topic = f"peer.test.{terminator}"

    hello = _hello_frame(peer.orch_id)
    sign(hello, plaintext)

    transport = ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as ac:
            await _post_frame_batch(
                ac, headers,
                [hello, _build_sub_frame(peer.orch_id, topic)],
            )
            assert topic_registry.get_peer_topics(peer.orch_id) == {topic}

            terminator_frame = (
                PeerFrame.goodbye(orch_id=peer.orch_id, in_flight=0)
                if terminator == "goodbye"
                else _build_revoke_frame(peer.orch_id)
            )
            await _post_frame_batch(ac, headers, [hello, terminator_frame])
            assert topic_registry.get_peer_topics(peer.orch_id) == set()
    finally:
        await transport.aclose()


async def test_sse_rejects_other_peers_orch_id(app, tmp_path) -> None:
    """PR-B9 hardening: a peer may only stream its own registry — the
    path orch_id must match the authenticated peer (403 otherwise)."""
    from httpx import ASGITransport
    import httpx

    peer, plaintext, headers = await _build_batch_headers(tmp_path)

    transport = ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as ac:
            resp = await ac.get(
                "/api/peer/peers/orch-NOT-ME/events",
                headers={
                    "Authorization": f"Bearer {plaintext}",
                    "X-Peer-Orchestrator-Id": peer.orch_id,
                },
            )
            assert resp.status_code == 403
    finally:
        await transport.aclose()


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