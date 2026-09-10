"""Peer federation API surface (PR2 discovery + PR5 trust & invoke).

The §5 handshake maps onto these endpoints:

1. ``GET  /.well-known/agent.json``                      — public discovery (PR2)
2. ``POST /api/peer/invite``                             — remote daemon applies (202)
3. ``POST /api/peer/invite/{peer_id}/accept``            — operator accept → per-peer token
4. ``POST /api/peer/invite/{peer_id}/reject``            — operator reject (hard delete)
5. ``GET  /api/peer/peers``                              — operator list (workspace-scoped)
6. ``DELETE /api/peer/peers/{orch_id}``                  — operator remove (AC8)
7. ``POST /api/peer/peers/{orch_id}/rotate-token``       — operator rotate (D15/NG8 grace)
8. ``POST /api/peer/peers/{orch_id}/invoke``             — peer INVOKE (AC6, D18/D19)
9. ``POST /api/peer/peers/{orch_id}/sessions``           — cross-daemon session (D17)
10. ``GET  /api/peer/peers/{orch_id}/events``            — SSE stream (AC7)
11. ``GET/PUT /api/peer/config``                         — auto-schedule 开关 (NG4)

Discovery is intentionally public — it precedes any shared token and
carries no workspace data — so this router mounts without
``require_auth`` (like ``realtime``). Remote-daemon endpoints are gated
by :func:`orchestratord.api.deps.require_peer_auth` (D25 rate limit);
operator endpoints use the standard ``require_auth``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field
from typing import Literal

from orchestratord.api.db import get_repositories
from orchestratord.api.deps import require_auth, require_peer_auth
from orchestratord.api.realtime import get_broker
from orchestratord.db import models as orm
from orchestratord.db.repository import Repositories
from orchestratord.domain.auth_token import issue_api_token
from orchestratord.peer.card import _resolve_card_url, build_agent_card, ensure_orch_id
from orchestratord.peer.dispatcher import PeerMessageDispatcher
from orchestratord.peer.registry import (
    CLIENT_KIND_V2,
    STATUS_ACCEPTED,
    STATUS_PENDING,
    get_peer,
    list_peers,
    remove_peer,
    set_peer_status,
    upsert_peer,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["peer"])

# D14: a peer relationship is workspace-scoped, so every invite names
# the workspace it wants to join.
_PEER_INVOKE_METHODS = {
    "POST /api/sessions/{session_id}/messages",
    "GET /api/workspaces/{workspace_id}/sessions",
}


@router.get("/.well-known/agent.json")
async def get_agent_card() -> dict:
    # PR-B1: defer URL resolution to ``peer.card._resolve_card_url`` so the
    # ``url`` field and the ``transports[rest].url`` field share one source
    # of truth (env → ORCHESTRATORD_PEER_LISTEN → sentinel default).
    return build_agent_card(orch_id=ensure_orch_id(), url=_resolve_card_url())


# ---------------------------------------------------------------------------
# Invite handshake (§5: apply → operator accept/reject)
# ---------------------------------------------------------------------------


class _InviteRequest(BaseModel):
    orch_id: str = Field(..., min_length=1)
    name: str = Field(..., min_length=1)
    url: str = Field(..., min_length=1)
    workspace_id: UUID
    capabilities: list[str] = Field(default_factory=list)
    card: dict[str, Any] | None = None
    # PR-B1: opt-in client-version tag. v2 clients advertise themselves;
    # Phase 1 clients omit the field and are stamped ``v1_sunset`` by
    # ``upsert_peer``. Pydantic v2 ``Literal`` keeps the wire schema
    # tight so a typo (``"v3"``) is rejected before the registry sees it.
    peer_client_version: Literal["v2"] | None = None


@router.post("/api/peer/invite", status_code=202)
async def post_invite(
    body: _InviteRequest,
    repos: Repositories = Depends(get_repositories),
) -> dict:
    """A remote daemon applies to join a workspace (§5 step 2).

    Creates a ``pending`` registry row and drops a ``peer_invite_request``
    into the operator inbox. When the caller's orch_id is on the
    ``ORCHESTRATORD_PEER_TRUST`` whitelist (R10), the invite is accepted
    immediately and the one-time per-peer token is returned in the same
    response (200 instead of 202).
    """
    peer = await upsert_peer(
        repos.session,
        workspace_id=body.workspace_id,
        orch_id=body.orch_id,
        name=body.name,
        url=body.url,
        capabilities=body.capabilities,
        card=body.card,
        # PR-B1: a v2 client opts in by sending peer_client_version="v2";
        # Phase 1 clients omit the field and ``upsert_peer`` defaults
        # to ``v1_sunset``. UPDATE path preserves the existing value
        # (see registry.upsert_peer docstring).
        client_kind=(
            CLIENT_KIND_V2
            if body.peer_client_version == "v2"
            else None
        ),
    )
    trust = {
        item.strip()
        for item in os.environ.get("ORCHESTRATORD_PEER_TRUST", "").split(",")
        if item.strip()
    }
    if body.orch_id in trust:
        plaintext, token_hash = issue_api_token()
        token_row = orm.AuthToken(
            id=uuid.uuid4(),
            workspace_id=body.workspace_id,
            name=f"peer:{body.orch_id}",
            token_hash=token_hash,
            scopes=["peer.*"],
            expires_at=None,
            created_at=datetime.now(UTC),
        )
        await repos.auth_tokens.add(token_row)
        # Link the token to the peer row (same as the accept endpoint) —
        # require_peer_auth resolves the caller via (token_id, orch_id).
        peer.token_id = token_row.id
        await set_peer_status(repos.session, peer, STATUS_ACCEPTED)
        # 200 (not the decorator's 202): the caller is pre-trusted, so
        # the handshake completes in this response (§5 / R10).
        return JSONResponse(
            status_code=200,
            content={
                "status": STATUS_ACCEPTED,
                "peer_id": str(peer.id),
                "token": plaintext,
            },
        )
    await repos.inbox.add(
        orm.InboxItem(
            id=uuid.uuid4(),
            workspace_id=body.workspace_id,
            kind="peer_invite_request",
            title=f"Peer {body.name} ({body.orch_id}) requests access",
            status="open",
            created_at=datetime.now(UTC),
        )
    )
    return {"status": STATUS_PENDING, "peer_id": str(peer.id)}


async def _pending_peer_or_404(repos: Repositories, peer_id: UUID):
    peer = await repos.session.get(orm.Peer, peer_id)
    if peer is None:
        raise HTTPException(status_code=404, detail="peer invite not found")
    return peer


@router.post("/api/peer/invite/{peer_id}/accept")
async def post_invite_accept(
    peer_id: UUID,
    repos: Repositories = Depends(get_repositories),
    _token: object = Depends(require_auth),
) -> dict:
    """Operator accept: issue the per-peer bearer token (D15, shown once)."""
    peer = await _pending_peer_or_404(repos, peer_id)
    plaintext, token_hash = issue_api_token()
    token_row = orm.AuthToken(
        id=uuid.uuid4(),
        workspace_id=peer.workspace_id,
        name=f"peer:{peer.orch_id}",
        token_hash=token_hash,
        scopes=["peer.*"],
        expires_at=None,
        created_at=datetime.now(UTC),
    )
    await repos.auth_tokens.add(token_row)
    peer.token_id = token_row.id
    await set_peer_status(repos.session, peer, STATUS_ACCEPTED)
    return {
        "status": STATUS_ACCEPTED,
        "orch_id": peer.orch_id,
        "token": plaintext,
    }


@router.post("/api/peer/invite/{peer_id}/reject")
async def post_invite_reject(
    peer_id: UUID,
    repos: Repositories = Depends(get_repositories),
    _token: object = Depends(require_auth),
) -> dict:
    """Operator reject: hard-delete the pending row (re-invite starts fresh)."""
    peer = await _pending_peer_or_404(repos, peer_id)
    await repos.session.delete(peer)
    await repos.session.flush()
    return {"status": "rejected", "orch_id": peer.orch_id}


# ---------------------------------------------------------------------------
# Operator registry views
# ---------------------------------------------------------------------------


@router.get("/api/peer/peers")
async def get_peers(
    workspace_id: UUID = Query(...),
    status: str | None = Query(None),
    repos: Repositories = Depends(get_repositories),
    _token: object = Depends(require_auth),
) -> list[dict]:
    rows = await list_peers(repos.session, workspace_id, status=status)
    return [
        {
            "id": str(row.id),
            "orch_id": row.orch_id,
            "name": row.name,
            "url": row.url,
            "status": row.status,
            "capabilities": row.capabilities,
            "client_kind": row.client_kind,  # PR-B1
            "created_at": row.created_at.isoformat(),
            "accepted_at": row.accepted_at.isoformat() if row.accepted_at else None,
        }
        for row in rows
    ]


@router.delete("/api/peer/peers/{orch_id}")
async def delete_peer(
    orch_id: str,
    workspace_id: UUID = Query(...),
    repos: Repositories = Depends(get_repositories),
    _token: object = Depends(require_auth),
) -> dict:
    """Remove a peer (AC8: removed peers lose access with the row)."""
    removed = await remove_peer(repos.session, workspace_id, orch_id)
    if not removed:
        raise HTTPException(status_code=404, detail="peer not found")
    return {"status": "removed", "orch_id": orch_id}


@router.post("/api/peer/peers/{orch_id}/rotate-token")
async def post_rotate_peer_token(
    orch_id: str,
    workspace_id: UUID = Query(...),
    repos: Repositories = Depends(get_repositories),
    _token: object = Depends(require_auth),
) -> dict:
    """Rotate the per-peer bearer token with a grace period (D15/NG8).

    Issues a fresh token (shown once in this response), rebinds the
    peer row to it, and keeps the OLD token alive for
    ``PeerConfig.token_grace_seconds`` (env
    ``ORCHESTRATORD_PEER_TOKEN_GRACE_SECONDS``, default 300) so
    in-flight remote connections drain instead of 401-ing mid-rotation.
    After the grace window the old ``auth_tokens`` row expires via the
    standard ``expires_at`` check in ``require_peer_auth`` — no extra
    revocation machinery.
    """
    peer = await get_peer(repos.session, workspace_id, orch_id)
    if peer is None or peer.status != STATUS_ACCEPTED:
        raise HTTPException(
            status_code=404, detail="accepted peer not found"
        )
    from orchestratord.config.schema import PeerConfig

    grace_seconds = PeerConfig.from_env().token_grace_seconds
    old_expires_at: datetime | None = None
    if peer.token_id is not None:
        old_token = await repos.session.get(orm.AuthToken, peer.token_id)
        if old_token is not None:
            old_expires_at = datetime.now(UTC) + timedelta(
                seconds=grace_seconds
            )
            old_token.expires_at = old_expires_at
    plaintext, token_hash = issue_api_token()
    token_row = orm.AuthToken(
        id=uuid.uuid4(),
        workspace_id=peer.workspace_id,
        name=f"peer:{peer.orch_id}",
        token_hash=token_hash,
        scopes=["peer.*"],
        expires_at=None,
        created_at=datetime.now(UTC),
    )
    await repos.auth_tokens.add(token_row)
    peer.token_id = token_row.id
    await repos.session.flush()
    return {
        "status": "rotated",
        "orch_id": peer.orch_id,
        "token": plaintext,
        "grace_seconds": grace_seconds,
        "old_token_expires_at": (
            old_expires_at.isoformat() if old_expires_at else None
        ),
    }


# ---------------------------------------------------------------------------
# Remote-daemon surface (require_peer_auth; D25 rate limit inside)
# ---------------------------------------------------------------------------


class _PeerInvoke(BaseModel):
    method: str = Field(..., description="peer/1 INVOKE method")
    body: dict[str, Any] = Field(default_factory=dict)
    msg_id: str | None = None
    ordering: str | None = None
    session_id: UUID | None = None


def _get_dispatcher() -> PeerMessageDispatcher:
    global _dispatcher
    if _dispatcher is None:
        _dispatcher = PeerMessageDispatcher(turn_scheduler=_default_turn_scheduler)
    return _dispatcher


_dispatcher: PeerMessageDispatcher | None = None


def reset_dispatcher() -> None:
    """Drop the process-wide dispatcher (test seam)."""
    global _dispatcher
    _dispatcher = None


async def _default_turn_scheduler(session_id: str, payload: dict[str, Any]) -> None:
    """§6.1d: hand the persisted remote message to the chat dispatcher.

    The session was persisted ``pending``; the daemon-side chat
    dispatcher's claim loop owns the actual turn. Waking it skips the
    poll interval so a peer auto-scheduled turn starts immediately.
    """
    from orchestratord.chat_daemon import live_chat_dispatcher

    dispatcher = live_chat_dispatcher()
    if dispatcher is not None:
        dispatcher.wake()
    logger.info("peer auto-scheduled turn on session %s", session_id)


async def _peer_invoke_message(
    repos: Repositories,
    peer: object,
    body: _PeerInvoke,
    out_of_order: bool,
) -> dict[str, Any]:
    """Persist a remote message (AC6) and write the D17 audit row."""
    session = await repos.session.get(orm.Session, body.session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="session not found")
    if session.workspace_id != peer.workspace_id:
        # D14: a peer relationship is workspace-scoped — a peer trusted
        # in workspace A must not write into workspace B sessions.
        raise HTTPException(
            status_code=403, detail="peer is not trusted in that workspace"
        )
    message = orm.Message(
        id=uuid.uuid4(),
        session_id=session.id,
        workspace_id=session.workspace_id,
        seq=0,  # sentinel — repository auto-assigns the next seq
        role=str(body.body.get("role", "user")),
        content=str(body.body.get("content", "")),
        agent_id=None,
        author_label=peer.orch_id,
        created_at=datetime.now(UTC),
    )
    await repos.messages.append(message)
    await repos.audit_log.add(
        orm.AuditLogEntry(
            id=uuid.uuid4(),
            workspace_id=session.workspace_id,
            actor_type="system",
            actor_id=peer.orch_id,
            action="peer.invoke.message",
            target_type="session",
            target_id=str(session.id),
            payload_jsonb={
                "peer_call_id": body.msg_id,
                "method": body.method,
                "out_of_order": out_of_order,
            },
            invited_by_orch_id=peer.orch_id,
            invited_by_peer_call_id=body.msg_id,
            created_at=datetime.now(UTC),
        )
    )
    return {"message_id": str(message.id), "seq": message.seq}


@router.post("/api/peer/peers/{orch_id}/invoke")
async def post_peer_invoke(
    orch_id: str,
    body: _PeerInvoke,
    repos: Repositories = Depends(get_repositories),
    peer: object = Depends(require_peer_auth),
) -> dict:
    """Handle an inbound peer INVOKE (AC6; D18 dedup + D19 ordering)."""
    if body.method not in _PEER_INVOKE_METHODS:
        raise HTTPException(status_code=422, detail=f"unsupported method {body.method!r}")
    if body.method == "GET /api/workspaces/{workspace_id}/sessions":
        # Read-only method: dedup-cache it too, but ordering is n/a.
        dispatcher = _get_dispatcher()
        cached = dispatcher.cached_result(orch_id, body.msg_id or "")
        if cached is not None:
            return {"status": 200, "body": cached, "duplicate": True}
        try:
            target_ws = UUID(str(body.body.get("workspace_id", "")))
        except ValueError as exc:
            # A malformed UUID from the remote is bad input (422), not a
            # server fault (500).
            raise HTTPException(
                status_code=422, detail="body.workspace_id must be a UUID"
            ) from exc
        if target_ws != peer.workspace_id:
            raise HTTPException(
                status_code=403, detail="peer is not trusted in that workspace"
            )
        sessions = await repos.sessions.list(workspace_id=target_ws)
        result = [
            {"id": str(s.id), "status": s.status, "mode": s.mode} for s in sessions
        ]
        dispatcher.remember_result(orch_id, body.msg_id or "", result)
        return {"status": 200, "body": result, "duplicate": False}

    if body.session_id is None:
        raise HTTPException(status_code=422, detail="session_id is required")
    msg_id = body.msg_id or str(uuid.uuid4())
    dispatcher = _get_dispatcher()
    cached = dispatcher.cached_result(orch_id, msg_id)
    if cached is not None:
        return {"status": 200, "body": cached, "duplicate": True}
    # D19: check before dispatch so the audit row can carry the flag;
    # dispatch_message then skips its own (stateful) re-check.
    out_of_order = body.ordering is not None and not dispatcher.check_ordering(
        orch_id, str(body.session_id), body.ordering
    )
    outcome = await dispatcher.dispatch_message(
        orch_id=orch_id,
        msg_id=msg_id,
        session_id=None,
        ordering=None,
        payload=body.body,
        execute=lambda: _peer_invoke_message(repos, peer, body, out_of_order),
    )
    result = dispatcher.cached_result(orch_id, msg_id)
    # Fresh dispatch → 201 Created; a D18 replay → 200 with the cached body.
    return JSONResponse(
        status_code=200 if outcome.duplicate else 201,
        content={
            "body": result,
            "duplicate": outcome.duplicate,
            "out_of_order": out_of_order,
            "scheduled": outcome.scheduled,
        },
    )


class _PeerSessionCreate(BaseModel):
    workspace_id: UUID
    prompt: str = Field(..., min_length=1)
    agent_id: UUID | None = None
    msg_id: str | None = None


@router.post("/api/peer/peers/{orch_id}/sessions", status_code=201)
async def post_peer_session(
    orch_id: str,
    body: _PeerSessionCreate,
    repos: Repositories = Depends(get_repositories),
    peer: object = Depends(require_peer_auth),
) -> dict:
    """Cross-daemon session creation (D17 / ADR D13).

    The workspace must be the one this peer was accepted into. The
    audit row carries ``invited_by_orch_id`` +
    ``invited_by_peer_call_id`` so the creating daemon is traceable
    (§7 R7). Sessions start ``pending`` — the local chat dispatcher
    owns the actual turn, exactly like ``POST .../chat/sessions``.
    """
    if body.workspace_id != peer.workspace_id:
        raise HTTPException(
            status_code=403, detail="peer is not trusted in that workspace"
        )
    session_id = uuid.uuid4()
    await repos.sessions.add(
        orm.Session(
            id=session_id,
            workspace_id=body.workspace_id,
            issue_id=None,
            agent_id=body.agent_id,
            run_id=None,
            mode="single",
            status="pending",
            created_at=datetime.now(UTC),
        )
    )
    message = orm.Message(
        id=uuid.uuid4(),
        session_id=session_id,
        workspace_id=body.workspace_id,
        seq=0,
        role="user",
        content=body.prompt,
        agent_id=None,
        author_label=peer.orch_id,
        created_at=datetime.now(UTC),
    )
    await repos.messages.append(message)
    msg_id = body.msg_id or str(uuid.uuid4())
    await repos.audit_log.add(
        orm.AuditLogEntry(
            id=uuid.uuid4(),
            workspace_id=body.workspace_id,
            actor_type="system",
            actor_id=peer.orch_id,
            action="peer.session.create",
            target_type="session",
            target_id=str(session_id),
            payload_jsonb={"peer_call_id": msg_id},
            invited_by_orch_id=peer.orch_id,
            invited_by_peer_call_id=msg_id,
            created_at=datetime.now(UTC),
        )
    )
    return {
        "session_id": str(session_id),
        "workspace_id": str(body.workspace_id),
        "status": "pending",
    }


@router.get("/api/peer/peers/{orch_id}/events")
async def get_peer_events(
    orch_id: str,
    topics: str = Query(
        "", description="Comma-separated topic list; only peer.* topics are served"
    ),
    peer: object = Depends(require_peer_auth),
) -> StreamingResponse:
    """SSE stream of ``peer.*`` broker topics (AC7, R11 isolation).

    Internal topics are refused — a remote peer can only ever observe
    the ``peer.`` namespace, so workspace-internal events never leak
    across the federation boundary.
    """
    wanted = {
        topic.strip()
        for topic in topics.split(",")
        if topic.strip().startswith("peer.")
    }
    if not wanted:
        raise HTTPException(status_code=422, detail="no peer.* topics requested")

    broker = get_broker()
    sub_id, frame_iter = await broker.subscribe(wanted)

    async def stream() -> AsyncIterator[str]:
        try:
            while True:
                try:
                    frame = await asyncio.wait_for(frame_iter.__anext__(), timeout=15.0)
                except TimeoutError:
                    yield ": keep-alive\n\n"
                    continue
                except StopAsyncIteration:
                    return
                yield f"data: {json.dumps(frame, ensure_ascii=False)}\n\n"
        finally:
            await broker.unsubscribe(sub_id)

    return StreamingResponse(
        stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"}
    )


# ---------------------------------------------------------------------------
# Operator config: the auto-schedule 开关 (NG4)
# ---------------------------------------------------------------------------


@router.get("/api/peer/config")
async def get_peer_config(
    _token: object = Depends(require_auth),
) -> dict:
    dispatcher = _get_dispatcher()
    return {
        "auto_schedule": dispatcher.auto_schedule,
        "max_concurrent_peer_turns": dispatcher._config.max_concurrent_peer_turns,
        "dedup_window_seconds": dispatcher._config.dedup_window_seconds,
    }


class _PeerConfigUpdate(BaseModel):
    auto_schedule: bool


@router.put("/api/peer/config")
async def put_peer_config(
    body: _PeerConfigUpdate,
    _token: object = Depends(require_auth),
) -> dict:
    dispatcher = _get_dispatcher()
    dispatcher.set_auto_schedule(body.auto_schedule)
    return {"auto_schedule": dispatcher.auto_schedule}
