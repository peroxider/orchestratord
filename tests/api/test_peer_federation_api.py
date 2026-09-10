"""Peer federation API surface tests (DESIGN §5/§6; AC6-AC8, D17, D25).

Drives the real FastAPI app over the live ``orchestratord_test`` database:
the invite handshake goes through ``POST /api/peer/invite`` + operator
``accept`` exactly as a remote daemon would, and the returned one-time
token then authenticates the ``require_peer_auth`` endpoints (invoke,
sessions, events). ``require_auth`` is lifted by the shared ``client``
fixture (operator endpoints are exercised unauthenticated); peer auth
runs for real.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import select

from orchestratord.api.deps import reset_peer_rate_bucket
from orchestratord.api.realtime import get_broker
from orchestratord.api.routers import peer as peer_router
from orchestratord.db import models as orm
from orchestratord.db.repository import Repositories

pytestmark = pytest.mark.database

PEER_HEADERS = {"X-Peer-Orchestrator-Id": "orch-A1"}


@pytest.fixture(autouse=True)
def fresh_peer_singletons():
    """Isolate the D18/D19 dispatcher and D25 bucket across tests."""
    peer_router.reset_dispatcher()
    reset_peer_rate_bucket()
    yield
    peer_router.reset_dispatcher()
    reset_peer_rate_bucket()


async def _seed_workspace(db) -> orm.Workspace:
    ws = orm.Workspace(
        id=uuid4(),
        slug=f"ws-{uuid4().hex[:8]}",
        name="Peer WS",
        created_at=datetime.now(UTC),
    )
    await Repositories(db).workspaces.add(ws)
    await db.commit()
    return ws


async def _seed_session(db, workspace_id) -> orm.Session:
    session = orm.Session(
        id=uuid4(),
        workspace_id=workspace_id,
        issue_id=None,
        agent_id=None,
        run_id=None,
        mode="single",
        status="running",
        created_at=datetime.now(UTC),
    )
    await Repositories(db).sessions.add(session)
    await db.commit()
    return session


async def _invite(client, workspace_id, orch_id="orch-A1") -> dict:
    resp = await client.post(
        "/api/peer/invite",
        json={
            "orch_id": orch_id,
            "name": "Remote A1",
            "url": "https://a1.example.com",
            "workspace_id": str(workspace_id),
            "capabilities": ["peer.invoke"],
        },
    )
    assert resp.status_code in (200, 202), resp.text
    return resp.json()


async def _accepted_peer(client, db, orch_id="orch-A1"):
    """Drive the full §5 handshake; return (workspace, session, token)."""
    ws = await _seed_workspace(db)
    body = await _invite(client, ws.id, orch_id)
    accept = await client.post(f"/api/peer/invite/{body['peer_id']}/accept")
    assert accept.status_code == 200, accept.text
    token = accept.json()["token"]
    session = await _seed_session(db, ws.id)
    return ws, session, token


def _peer_headers(token: str) -> dict:
    return {**PEER_HEADERS, "Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# §5 invite handshake
# ---------------------------------------------------------------------------


async def test_invite_creates_pending_row_and_inbox_item(client, db) -> None:
    ws = await _seed_workspace(db)
    body = await _invite(client, ws.id)
    assert body["status"] == "pending"

    repos = Repositories(db)
    peer = await repos.session.get(orm.Peer, uuid.UUID(body["peer_id"]))
    assert peer is not None and peer.status == "pending"
    inbox = (
        (
            await db.execute(
                select(orm.InboxItem).where(
                    orm.InboxItem.kind == "peer_invite_request"
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(inbox) == 1
    assert "orch-A1" in inbox[0].title


async def test_reinvite_refreshes_fields_without_resurrecting_trust(
    client, db
) -> None:
    ws = await _seed_workspace(db)
    first = await _invite(client, ws.id)
    accept = await client.post(f"/api/peer/invite/{first['peer_id']}/accept")
    assert accept.status_code == 200
    second = await _invite(client, ws.id, orch_id="orch-A1")
    assert second["status"] == "pending"  # discovery refresh, trust untouched
    assert second["peer_id"] == first["peer_id"]


async def test_trust_whitelist_accepts_immediately(
    client, db, monkeypatch
) -> None:
    monkeypatch.setenv("ORCHESTRATORD_PEER_TRUST", "orch-trusted, other")
    ws = await _seed_workspace(db)
    resp = await client.post(
        "/api/peer/invite",
        json={
            "orch_id": "orch-trusted",
            "name": "Trusted",
            "url": "https://t.example.com",
            "workspace_id": str(ws.id),
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "accepted"
    assert body["token"]  # D15 one-time token in the same response

    # The trust-issued token must work immediately (regression: the
    # trust branch once skipped the peer.token_id linkage, so
    # require_peer_auth rejected every pre-trusted peer).
    session = await _seed_session(db, workspace_id=ws.id)
    invoke = await client.post(
        "/api/peer/peers/orch-trusted/invoke",
        headers={
            "Authorization": f"Bearer {body['token']}",
            "X-Peer-Orchestrator-Id": "orch-trusted",
        },
        json={
            "method": "POST /api/sessions/{session_id}/messages",
            "body": {"role": "user", "content": "hello"},
            "session_id": str(session.id),
            "msg_id": "trust-1",
        },
    )
    assert invoke.status_code == 201, invoke.text
    assert invoke.json()["duplicate"] is False


async def test_accept_issues_token_and_marks_accepted(client, db) -> None:
    ws = await _seed_workspace(db)
    body = await _invite(client, ws.id)
    resp = await client.post(f"/api/peer/invite/{body['peer_id']}/accept")
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["status"] == "accepted"
    assert payload["orch_id"] == "orch-A1"
    assert payload["token"]
    peer = await Repositories(db).session.get(
        orm.Peer, uuid.UUID(body["peer_id"])
    )
    assert peer.status == "accepted"
    assert peer.token_id is not None


async def test_reject_hard_deletes_pending_row(client, db) -> None:
    ws = await _seed_workspace(db)
    body = await _invite(client, ws.id)
    resp = await client.post(f"/api/peer/invite/{body['peer_id']}/reject")
    assert resp.status_code == 200
    assert resp.json()["status"] == "rejected"
    assert (
        await Repositories(db).session.get(orm.Peer, uuid.UUID(body["peer_id"]))
        is None
    )
    again = await client.post(f"/api/peer/invite/{body['peer_id']}/reject")
    assert again.status_code == 404


async def test_list_peers_filters_by_status(client, db) -> None:
    ws = await _seed_workspace(db)
    await _invite(client, ws.id, orch_id="orch-A1")
    await _invite(client, ws.id, orch_id="orch-B2")

    pending = (
        await client.get(
            "/api/peer/peers",
            params={"workspace_id": str(ws.id), "status": "pending"},
        )
    ).json()
    assert [p["orch_id"] for p in pending] == ["orch-A1", "orch-B2"]

    await client.post(f"/api/peer/invite/{pending[0]['id']}/accept")
    accepted = (
        await client.get(
            "/api/peer/peers",
            params={"workspace_id": str(ws.id), "status": "accepted"},
        )
    ).json()
    assert [p["orch_id"] for p in accepted] == ["orch-A1"]


async def test_remove_peer_revokes_access_ac8(client, db) -> None:
    ws, _session, token = await _accepted_peer(client, db)
    resp = await client.delete(
        "/api/peer/peers/orch-A1", params={"workspace_id": str(ws.id)}
    )
    assert resp.status_code == 200
    # AC8: the removed peer's token no longer authenticates anything.
    denied = await client.post(
        "/api/peer/peers/orch-A1/invoke",
        headers=_peer_headers(token),
        json={
            "method": "GET /api/workspaces/{workspace_id}/sessions",
            "body": {"workspace_id": str(ws.id)},
            "msg_id": "m-post-remove",
        },
    )
    assert denied.status_code == 401
    missing = await client.delete(
        "/api/peer/peers/orch-A1", params={"workspace_id": str(ws.id)}
    )
    assert missing.status_code == 404


# ---------------------------------------------------------------------------
# INVOKE (AC6; D18 dedup, D19 ordering, D14 workspace boundary)
# ---------------------------------------------------------------------------


async def test_invoke_persists_message_and_audit_d17(client, db) -> None:
    _ws, session, token = await _accepted_peer(client, db)
    resp = await client.post(
        "/api/peer/peers/orch-A1/invoke",
        headers=_peer_headers(token),
        json={
            "method": "POST /api/sessions/{session_id}/messages",
            "body": {"role": "user", "content": "hello from A1"},
            "msg_id": "m-1",
            "session_id": str(session.id),
            "ordering": "1",
        },
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["duplicate"] is False
    assert body["out_of_order"] is False

    messages = (
        (
            await db.execute(
                select(orm.Message).where(orm.Message.session_id == session.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(messages) == 1
    assert messages[0].content == "hello from A1"
    assert messages[0].author_label == "orch-A1"

    audits = (
        (
            await db.execute(
                select(orm.AuditLogEntry).where(
                    orm.AuditLogEntry.action == "peer.invoke.message"
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(audits) == 1
    assert audits[0].invited_by_orch_id == "orch-A1"
    assert audits[0].invited_by_peer_call_id == "m-1"
    assert audits[0].payload_jsonb["out_of_order"] is False


async def test_invoke_replay_is_served_from_dedup_window(client, db) -> None:
    _ws, session, token = await _accepted_peer(client, db)
    payload = {
        "method": "POST /api/sessions/{session_id}/messages",
        "body": {"role": "user", "content": "at-least-once"},
        "msg_id": "m-dup",
        "session_id": str(session.id),
    }
    first = await client.post(
        "/api/peer/peers/orch-A1/invoke", headers=_peer_headers(token), json=payload
    )
    replay = await client.post(
        "/api/peer/peers/orch-A1/invoke", headers=_peer_headers(token), json=payload
    )
    assert first.status_code == 201
    assert replay.status_code == 200
    assert replay.json()["duplicate"] is True
    assert replay.json()["body"] == first.json()["body"]
    messages = (
        (
            await db.execute(
                select(orm.Message).where(orm.Message.session_id == session.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(messages) == 1  # D18: the replay never re-executes


async def test_invoke_ordering_gap_flags_out_of_order(client, db) -> None:
    _ws, session, token = await _accepted_peer(client, db)

    async def send(msg_id: str, ordering: str) -> dict:
        resp = await client.post(
            "/api/peer/peers/orch-A1/invoke",
            headers=_peer_headers(token),
            json={
                "method": "POST /api/sessions/{session_id}/messages",
                "body": {"role": "user", "content": msg_id},
                "msg_id": msg_id,
                "session_id": str(session.id),
                "ordering": ordering,
            },
        )
        assert resp.status_code in (200, 201)
        return resp.json()

    assert (await send("m-1", "1"))["out_of_order"] is False
    assert (await send("m-2", "5"))["out_of_order"] is True  # D19 gap
    audits = (
        (
            await db.execute(
                select(orm.AuditLogEntry).where(
                    orm.AuditLogEntry.action == "peer.invoke.message"
                )
            )
        )
        .scalars()
        .all()
    )
    assert [a.payload_jsonb["out_of_order"] for a in audits] == [False, True]


async def test_invoke_unsupported_method_422(client, db) -> None:
    _ws, _session, token = await _accepted_peer(client, db)
    resp = await client.post(
        "/api/peer/peers/orch-A1/invoke",
        headers=_peer_headers(token),
        json={"method": "DELETE /api/everything", "body": {}, "msg_id": "m-x"},
    )
    assert resp.status_code == 422


async def test_invoke_malformed_workspace_id_422(client, db) -> None:
    """A non-UUID workspace_id from the remote is bad input, not a 500."""
    _ws, _session, token = await _accepted_peer(client, db)
    resp = await client.post(
        "/api/peer/peers/orch-A1/invoke",
        headers=_peer_headers(token),
        json={
            "method": "GET /api/workspaces/{workspace_id}/sessions",
            "body": {"workspace_id": "not-a-uuid"},
            "msg_id": "m-bad-ws",
        },
    )
    assert resp.status_code == 422


async def test_invoke_unknown_session_404(client, db) -> None:
    _ws, _session, token = await _accepted_peer(client, db)
    resp = await client.post(
        "/api/peer/peers/orch-A1/invoke",
        headers=_peer_headers(token),
        json={
            "method": "POST /api/sessions/{session_id}/messages",
            "body": {"role": "user", "content": "ghost"},
            "msg_id": "m-ghost",
            "session_id": str(uuid4()),
        },
    )
    assert resp.status_code == 404


async def test_invoke_foreign_workspace_session_403_d14(client, db) -> None:
    """D14: a peer accepted into ws1 cannot write into ws2 sessions."""
    ws, _session, token = await _accepted_peer(client, db, orch_id="orch-A1")
    other_ws = await _seed_workspace(db)
    foreign = await _seed_session(db, other_ws.id)
    resp = await client.post(
        "/api/peer/peers/orch-A1/invoke",
        headers=_peer_headers(token),
        json={
            "method": "POST /api/sessions/{session_id}/messages",
            "body": {"role": "user", "content": "smuggle"},
            "msg_id": "m-smuggle",
            "session_id": str(foreign.id),
        },
    )
    assert resp.status_code == 403
    assert ws.id != other_ws.id


async def test_invoke_readonly_sessions_method_lists_workspace(
    client, db
) -> None:
    ws, _session, token = await _accepted_peer(client, db)
    resp = await client.post(
        "/api/peer/peers/orch-A1/invoke",
        headers=_peer_headers(token),
        json={
            "method": "GET /api/workspaces/{workspace_id}/sessions",
            "body": {"workspace_id": str(ws.id)},
            "msg_id": "m-list",
        },
    )
    assert resp.status_code == 200
    listings = resp.json()["body"]
    assert any(entry["id"] for entry in listings)


# ---------------------------------------------------------------------------
# Cross-daemon sessions (D17)
# ---------------------------------------------------------------------------


async def test_peer_session_create_persists_audit_invited_by(
    client, db
) -> None:
    ws, _session, token = await _accepted_peer(client, db)
    resp = await client.post(
        "/api/peer/peers/orch-A1/sessions",
        headers=_peer_headers(token),
        json={
            "workspace_id": str(ws.id),
            "prompt": "start a remote turn",
            "msg_id": "ms-1",
        },
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["status"] == "pending"

    created = await Repositories(db).session.get(
        orm.Session, uuid.UUID(body["session_id"])
    )
    assert created is not None and created.status == "pending"
    message = (
        (
            await db.execute(
                select(orm.Message).where(orm.Message.session_id == created.id)
            )
        )
        .scalars()
        .one()
    )
    assert message.content == "start a remote turn"
    audit = (
        (
            await db.execute(
                select(orm.AuditLogEntry).where(
                    orm.AuditLogEntry.action == "peer.session.create"
                )
            )
        )
        .scalars()
        .one()
    )
    assert audit.invited_by_orch_id == "orch-A1"
    assert audit.invited_by_peer_call_id == "ms-1"


async def test_peer_session_create_rejects_foreign_workspace(client, db) -> None:
    _ws, _session, token = await _accepted_peer(client, db)
    other_ws = await _seed_workspace(db)
    resp = await client.post(
        "/api/peer/peers/orch-A1/sessions",
        headers=_peer_headers(token),
        json={"workspace_id": str(other_ws.id), "prompt": "nope"},
    )
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# SSE events (AC7/R11) + config 开关 (NG4) + D25 rate limit
# ---------------------------------------------------------------------------


async def test_events_refuse_non_peer_topics(client, db) -> None:
    _ws, _session, token = await _accepted_peer(client, db)
    resp = await client.get(
        "/api/peer/peers/orch-A1/events",
        headers=_peer_headers(token),
        params={"topics": "session.events,run.logs"},
    )
    assert resp.status_code == 422


async def test_events_stream_delivers_only_peer_frames(client, db) -> None:
    _ws, _session, token = await _accepted_peer(client, db)

    async def publish_soon() -> None:
        await asyncio.sleep(0.05)
        await get_broker().publish(
            "peer.agent.1.events", {"kind": "agent.event", "text": "hi"}
        )

    publisher = asyncio.create_task(publish_soon())
    try:
        async with client.stream(
            "GET",
            "/api/peer/peers/orch-A1/events",
            headers=_peer_headers(token),
            params={"topics": "peer.agent.1.events"},
        ) as resp:
            assert resp.status_code == 200
            frame = None
            async for line in resp.aiter_lines():
                if line.startswith("data: "):
                    frame = json.loads(line[len("data: ") :])
                    break
            assert frame is not None
            assert frame["topic"] == "peer.agent.1.events"
            assert frame["payload"]["text"] == "hi"
    finally:
        publisher.cancel()
        try:
            await publisher
        except asyncio.CancelledError:
            pass


async def test_peer_config_auto_schedule_toggle(client) -> None:
    initial = (await client.get("/api/peer/config")).json()
    assert initial["auto_schedule"] is False  # NG4 default
    put = await client.put("/api/peer/config", json={"auto_schedule": True})
    assert put.status_code == 200
    assert (await client.get("/api/peer/config")).json()["auto_schedule"] is True


async def test_rate_limit_returns_429_with_retry_after(
    client, db, monkeypatch
) -> None:
    monkeypatch.setenv("ORCHESTRATORD_PEER_RATE_RPS", "0.001")
    monkeypatch.setenv("ORCHESTRATORD_PEER_RATE_BURST", "1")
    reset_peer_rate_bucket()
    ws, _session, token = await _accepted_peer(client, db)

    async def send(msg_id: str):
        return await client.post(
            "/api/peer/peers/orch-A1/invoke",
            headers=_peer_headers(token),
            json={
                "method": "GET /api/workspaces/{workspace_id}/sessions",
                "body": {"workspace_id": str(ws.id)},
                "msg_id": msg_id,
            },
        )

    first = await send("r-1")
    second = await send("r-2")
    assert first.status_code == 200
    assert second.status_code == 429  # D25
    assert "retry-after" in {k.lower() for k in second.headers}


# ---------------------------------------------------------------------------
# D15/NG8: token rotation with grace period
# ---------------------------------------------------------------------------


async def test_rotate_token_rebinds_and_grants_grace(client, db) -> None:
    """D15/NG8: rotate issues a new token, rebinds the peer row, and
    the OLD token keeps authenticating inside the grace window."""
    ws, _session, old_token = await _accepted_peer(client, db)
    resp = await client.post(
        f"/api/peer/peers/orch-A1/rotate-token?workspace_id={ws.id}"
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "rotated"
    assert body["orch_id"] == "orch-A1"
    new_token = body["token"]
    assert new_token and new_token != old_token
    assert body["grace_seconds"] == 300.0  # default config
    assert body["old_token_expires_at"] is not None

    # New token authenticates (invoke goes through require_peer_auth).
    fresh = await client.post(
        "/api/peer/peers/orch-A1/invoke",
        headers=_peer_headers(new_token),
        json={
            "method": "GET /api/workspaces/{workspace_id}/sessions",
            "body": {"workspace_id": str(ws.id)},
            "msg_id": "rot-new-1",
        },
    )
    assert fresh.status_code == 200, fresh.text

    # Old token still works during the grace window.
    old_ok = await client.post(
        "/api/peer/peers/orch-A1/invoke",
        headers=_peer_headers(old_token),
        json={
            "method": "GET /api/workspaces/{workspace_id}/sessions",
            "body": {"workspace_id": str(ws.id)},
            "msg_id": "rot-old-1",
        },
    )
    assert old_ok.status_code == 200, old_ok.text


async def test_rotate_old_token_dies_after_grace(
    client, db, monkeypatch
) -> None:
    """D15/NG8: with grace=0 the old token expires immediately — the
    next request 401s while the new token keeps working."""
    monkeypatch.setenv("ORCHESTRATORD_PEER_TOKEN_GRACE_SECONDS", "0")
    ws, _session, old_token = await _accepted_peer(client, db)
    resp = await client.post(
        f"/api/peer/peers/orch-A1/rotate-token?workspace_id={ws.id}"
    )
    assert resp.status_code == 200, resp.text
    new_token = resp.json()["token"]

    async def send(tok: str, msg_id: str):
        return await client.post(
            "/api/peer/peers/orch-A1/invoke",
            headers=_peer_headers(tok),
            json={
                "method": "GET /api/workspaces/{workspace_id}/sessions",
                "body": {"workspace_id": str(ws.id)},
                "msg_id": msg_id,
            },
        )

    assert (await send(old_token, "grace-dead")).status_code == 401
    assert (await send(new_token, "grace-new")).status_code == 200


async def test_rotate_unknown_peer_returns_404(client, db) -> None:
    ws = await _seed_workspace(db)
    resp = await client.post(
        f"/api/peer/peers/orch-NOPE/rotate-token?workspace_id={ws.id}"
    )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# PR-B1: peer_client_version → client_kind (Phase B v2 vs Phase 1 v1_sunset)
# ---------------------------------------------------------------------------


async def test_invite_without_peer_client_version_stamps_v1_sunset(
    client, db
) -> None:
    """PR-B1: a Phase 1 client (no peer_client_version) is auto-classified
    ``v1_sunset`` so operators can see the legacy population at a glance."""
    ws = await _seed_workspace(db)
    resp = await client.post(
        "/api/peer/invite",
        json={
            "orch_id": "orch-legacy",
            "name": "Legacy",
            "url": "http://legacy:9001",
            "workspace_id": str(ws.id),
            "capabilities": ["peer.invoke"],
        },
    )
    assert resp.status_code == 202, resp.text
    listed = await client.get(
        "/api/peer/peers",
        params={"workspace_id": str(ws.id)},
    )
    rows = listed.json()
    assert len(rows) == 1
    assert rows[0]["orch_id"] == "orch-legacy"
    assert rows[0]["client_kind"] == "v1_sunset"


async def test_invite_with_peer_client_version_v2_stamps_v2(
    client, db
) -> None:
    """PR-B1: a v2 client opting in via ``peer_client_version: "v2"`` is
    classified accordingly and survives the list round-trip."""
    ws = await _seed_workspace(db)
    resp = await client.post(
        "/api/peer/invite",
        json={
            "orch_id": "orch-modern",
            "name": "Modern",
            "url": "http://modern:9001",
            "workspace_id": str(ws.id),
            "capabilities": ["peer.invoke"],
            "peer_client_version": "v2",
        },
    )
    assert resp.status_code == 202, resp.text
    listed = await client.get(
        "/api/peer/peers",
        params={"workspace_id": str(ws.id)},
    )
    rows = listed.json()
    assert len(rows) == 1
    assert rows[0]["client_kind"] == "v2"


async def test_reinvite_without_version_preserves_existing_v2(
    client, db
) -> None:
    """PR-B1 invariant (server-side mirror of the registry test):
    a re-invite that omits ``peer_client_version`` must not downgrade
    a peer that was previously classified ``v2``."""
    ws = await _seed_workspace(db)
    first = await client.post(
        "/api/peer/invite",
        json={
            "orch_id": "orch-modern",
            "name": "Modern",
            "url": "http://modern:9001",
            "workspace_id": str(ws.id),
            "peer_client_version": "v2",
        },
    )
    assert first.status_code == 202
    # Re-invite without peer_client_version.
    second = await client.post(
        "/api/peer/invite",
        json={
            "orch_id": "orch-modern",
            "name": "Modern",
            "url": "http://modern:9001",
            "workspace_id": str(ws.id),
        },
    )
    assert second.status_code == 202
    listed = await client.get(
        "/api/peer/peers",
        params={"workspace_id": str(ws.id)},
    )
    rows = listed.json()
    assert len(rows) == 1
    assert rows[0]["client_kind"] == "v2"


async def test_invite_rejects_unknown_peer_client_version(client, db) -> None:
    """PR-B1: the Pydantic ``Literal["v2"]`` keeps the wire schema tight
    — typos like ``"v3"`` are rejected with 422 before the registry sees
    them, so a malformed version never silently downgrades a peer."""
    ws = await _seed_workspace(db)
    resp = await client.post(
        "/api/peer/invite",
        json={
            "orch_id": "orch-x",
            "name": "X",
            "url": "http://x:9001",
            "workspace_id": str(ws.id),
            "peer_client_version": "v3",  # not yet released
        },
    )
    assert resp.status_code == 422, resp.text
