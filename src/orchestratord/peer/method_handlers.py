"""peer/1 frame INVOKE method handlers (PR-B3).

Each handler signature is one of:

    async def handler(peer_row, body, repos) -> dict[str, Any]

or, for synchronous bridge-pending stubs:

    def handler(peer_row, body, repos) -> dict[str, Any]

Both return a JSON-serializable dict that becomes the RESULT frame
body. Handlers may raise :class:`fastapi.HTTPException` for client
errors (4xx); arbitrary ``Exception`` propagates and is translated by
``peer_frame._dispatch_invoke_frame`` (logged + 500 envelope).

The dispatch table :data:`PEER_FRAME_METHOD_HANDLERS` is the single
source of truth for the method-string → handler mapping on the frame
transport. It mirrors the REST peer-invoke surface but is independent
— PR-B3 deliberately widens the surface from REST's 2 methods to 6
(see plan §Design).

**Workspace authorization** is the first line of each handler — the
peer row already passed :func:`require_peer_auth` but defense-in-depth
requires re-checking the body's target ``workspace_id`` against the
peer's authorized workspace (D14). Cross-workspace writes are
explicitly forbidden.

**Mutating cross-process methods** (sessions.approve, agents.message)
return :data:`_NOT_BRIDGE_PENDING_BODY` (501 with "cross-process
bridge pending") so the wire semantics (status code, ``__error__``
translation) are tested end-to-end even before the real cross-daemon
bridge lands (PR-B4).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from fastapi import HTTPException

from orchestratord.db import models as orm
from orchestratord.db.repository import Repositories


def _require_workspace(body: dict[str, Any], peer_row: Any) -> UUID:
    """Extract and authorize the target workspace from the request body.

    Raises ``HTTPException(422)`` for a malformed UUID and
    ``HTTPException(403)`` for a workspace mismatch (D14). On success
    returns the parsed UUID so the caller can use it without re-parsing.
    """
    try:
        target = UUID(str(body.get("workspace_id", "")))
    except (ValueError, TypeError) as exc:
        raise HTTPException(
            status_code=422, detail="body.workspace_id must be a UUID"
        ) from exc
    if target != peer_row.workspace_id:
        raise HTTPException(
            status_code=403, detail="peer is not trusted in that workspace"
        )
    return target


# Methods whose ``frame.body`` carries a stable ``session_id`` key
# — these are the routes PR-B4 enables D19 ordering on. Read-only
# methods (sessions.read, agents.list, inbox.read) are deliberately
# excluded: there's no per-session write stream to order.
SESSION_ID_METHODS: frozenset[str] = frozenset({
    "POST /api/sessions/{session_id}/messages",
    "POST /api/sessions/{session_id}/approve",
})


def _extract_session_id(method: str, body: dict[str, Any]) -> str | None:
    """Return the per-session key for D19 ordering, or ``None``.

    For methods in :data:`SESSION_ID_METHODS` the key is
    ``body["session_id"]`` stringified (UUID → ``str(uuid)``); for any
    other method the return is ``None`` and the dispatcher's ordering
    check is short-circuited (``out_of_order`` always False).
    """
    if method not in SESSION_ID_METHODS:
        return None
    raw = body.get("session_id")
    return str(raw) if raw is not None else None


async def handle_sessions_read(
    peer_row: Any,
    body: dict[str, Any],
    repos: Repositories,
    *,
    msg_id: str = "",  # unused on this read path; uniform dispatcher signature
    out_of_order: bool = False,  # ditto
) -> list[dict[str, Any]]:
    """``GET /api/workspaces/{workspace_id}/sessions`` — list sessions.

    Mirrors the REST peer-invoke implementation at
    ``orchestratord.api.routers.peer`` lines 367–390. Read-only, so
    ordering is n/a and the dispatcher dedups via the body itself.
    """
    workspace_id = _require_workspace(body, peer_row)
    sessions = await repos.sessions.list(workspace_id=workspace_id)
    return [
        {"id": str(s.id), "status": s.status, "mode": s.mode}
        for s in sessions
    ]


async def handle_sessions_message_post(
    peer_row: Any,
    body: dict[str, Any],
    repos: Repositories,
    *,
    msg_id: str = "",
    out_of_order: bool = False,
) -> dict[str, Any]:
    """``POST /api/sessions/{session_id}/messages`` — persist a remote message.

    Mirrors :func:`orchestratord.api.routers.peer._peer_invoke_message`
    (REST path) but frame-native — the inbound ``frame.body`` is the
    full JSON payload, no Pydantic round-trip. Writes an :class:`AuditLogEntry`
    row with ``invited_by_orch_id == peer_row.orch_id`` so the creating
    daemon is traceable (§7 R7 / D17). The audit row's
    ``payload_jsonb`` matches the REST shape
    (``peer_call_id`` + ``method`` + ``out_of_order``, D19 / D17).
    """
    try:
        session_id = UUID(str(body.get("session_id", "")))
    except (ValueError, TypeError) as exc:
        raise HTTPException(
            status_code=422, detail="body.session_id must be a UUID"
        ) from exc
    session = await repos.session.get(orm.Session, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="session not found")
    if session.workspace_id != peer_row.workspace_id:
        raise HTTPException(
            status_code=403, detail="peer is not trusted in that workspace"
        )
    message = orm.Message(
        id=uuid.uuid4(),
        session_id=session.id,
        workspace_id=session.workspace_id,
        seq=0,  # sentinel — repository auto-assigns the next seq
        role=str(body.get("role", "user")),
        content=str(body.get("content", "")),
        agent_id=None,
        author_label=peer_row.orch_id,
        created_at=datetime.now(UTC),
    )
    await repos.messages.append(message)
    await repos.audit_log.add(
        orm.AuditLogEntry(
            id=uuid.uuid4(),
            workspace_id=session.workspace_id,
            actor_type="system",
            actor_id=peer_row.orch_id,
            action="peer.invoke.message",
            target_type="session",
            target_id=str(session.id),
            payload_jsonb={
                "peer_call_id": msg_id,
                "method": "POST /api/sessions/{session_id}/messages",
                "out_of_order": out_of_order,
            },
            invited_by_orch_id=peer_row.orch_id,
            invited_by_peer_call_id=msg_id or str(message.id),
            created_at=datetime.now(UTC),
        )
    )
    return {"message_id": str(message.id), "seq": message.seq}


async def handle_agents_list(
    peer_row: Any,
    body: dict[str, Any],
    repos: Repositories,
    *,
    msg_id: str = "",
    out_of_order: bool = False,
) -> list[dict[str, Any]]:
    """``GET /api/workspaces/{workspace_id}/agents`` — list agents."""
    workspace_id = _require_workspace(body, peer_row)
    agents = await repos.agents.list_for_workspace(workspace_id)
    return [
        {
            "id": str(a.id),
            "name": a.name,
            "provider": a.provider,
            "runtime_id": str(a.runtime_id),
        }
        for a in agents
    ]


async def handle_inbox_read(
    peer_row: Any,
    body: dict[str, Any],
    repos: Repositories,
    *,
    msg_id: str = "",
    out_of_order: bool = False,
) -> list[dict[str, Any]]:
    """``GET /api/workspaces/{workspace_id}/inbox`` — list inbox items."""
    workspace_id = _require_workspace(body, peer_row)
    items = await repos.inbox.list_for_workspace(workspace_id)
    return [
        {
            "id": str(i.id),
            "kind": i.kind,
            "title": i.title,
            "status": i.status,
            "issue_id": str(i.issue_id) if i.issue_id else None,
            "session_id": str(i.session_id) if i.session_id else None,
        }
        for i in items
    ]


def _not_bridge_pending(method: str) -> dict[str, Any]:
    """Bridge-pending stub body (501) for mutating cross-process methods.

    The dict shape (``__error__`` + ``__status__``) is what
    ``peer_frame._dispatch_invoke_frame`` recognizes as an error
    envelope and surfaces to the client as a RESULT frame with the
    right status code. Synchronous because there's nothing async to do
    — the bridge just isn't there yet.
    """
    return {
        "__error__": (
            f"method {method!r} requires the cross-process bridge "
            "which is not implemented on this build; use the REST "
            "peer.invoke path or wait for PR-B4"
        ),
        "__status__": 501,
    }


def handle_sessions_approve(
    peer_row: Any,
    body: dict[str, Any],
    repos: Repositories,
    *,
    msg_id: str = "",
    out_of_order: bool = False,
) -> dict[str, Any]:
    """``POST /api/sessions/{session_id}/approve`` — bridge-pending stub."""
    return _not_bridge_pending("POST /api/sessions/{session_id}/approve")


def handle_agents_message(
    peer_row: Any,
    body: dict[str, Any],
    repos: Repositories,
    *,
    msg_id: str = "",
    out_of_order: bool = False,
) -> dict[str, Any]:
    """``POST /api/agents/{agent_id}/message`` — bridge-pending stub."""
    return _not_bridge_pending("POST /api/agents/{agent_id}/message")


# Dispatch table: method-string → handler. ``PEER_FRAME_METHOD_HANDLERS``
# is the single source of truth on the frame transport — adding a new
# routed method is a one-line change here plus a handler. The REST
# peer's ``_PEER_INVOKE_METHODS`` set (peer.py:64–67) stays independent
# of this dict on purpose: REST has different authorization + transport
# constraints and a different release cadence.
PEER_FRAME_METHOD_HANDLERS: dict[str, Any] = {
    "GET /api/workspaces/{workspace_id}/sessions": handle_sessions_read,
    "POST /api/sessions/{session_id}/messages": handle_sessions_message_post,
    "POST /api/sessions/{session_id}/approve": handle_sessions_approve,
    "GET /api/workspaces/{workspace_id}/agents": handle_agents_list,
    "POST /api/agents/{agent_id}/message": handle_agents_message,
    "GET /api/workspaces/{workspace_id}/inbox": handle_inbox_read,
}


__all__ = [
    "PEER_FRAME_METHOD_HANDLERS",
    "SESSION_ID_METHODS",
    "handle_sessions_read",
    "handle_sessions_message_post",
    "handle_sessions_approve",
    "handle_agents_list",
    "handle_agents_message",
    "handle_inbox_read",
    "_not_bridge_pending",
    "_require_workspace",
    "_extract_session_id",
]