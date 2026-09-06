"""Sessions REST API (``docs/FEATURE_GAP_VS_MULTICA.md`` §5.2.3).

The execution-log / replay surface. A session is created by the backend
runner (``BackendRunner.run``), not by the Web client, so there is no create
endpoint here: the frontend lists sessions scoped to a workspace or issue and
then reads a session's ``events`` timeline, resolves ``APPROVAL_REQUEST``
events via approve/deny, and drives pause/resume/stop on the running session.

Sessions, events, and approvals persist via the repository layer (§6.1). The
SSE stream (``/events/stream``) and chat (``/messages``) endpoints are deferred
to the realtime/chat phases (§5.4 / §7.4).
"""

from __future__ import annotations

import base64
from datetime import UTC, datetime
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from orchestratord.api.db import get_repositories
from orchestratord.db import models as orm
from orchestratord.db.repository import Repositories
from orchestratord.spi.events import EventKind

router = APIRouter(tags=["sessions"])


def _session_payload(session: orm.Session) -> dict:
    return {
        "id": str(session.id),
        "workspace_id": str(session.workspace_id),
        "issue_id": str(session.issue_id) if session.issue_id else None,
        "agent_id": str(session.agent_id) if session.agent_id else None,
        "run_id": str(session.run_id) if session.run_id else None,
        "mode": session.mode,
        "status": session.status,
        "created_at": session.created_at.isoformat(),
    }


def _event_payload(event: orm.Event) -> dict:
    return {
        "seq": event.sequence,
        "timestamp": event.created_at.timestamp(),
        "kind": event.kind,
        "payload": event.payload,
    }


async def _session_or_404(repos: Repositories, session_id: UUID) -> orm.Session:
    session = await repos.sessions.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="session not found")
    return session


def _encode_cursor(seq: int) -> str:
    """Opaque pagination token carrying the last-seen seq (Phase-1 form)."""
    return base64.urlsafe_b64encode(str(seq).encode("ascii")).decode("ascii")


def _decode_cursor(cursor: str) -> int:
    try:
        return int(base64.urlsafe_b64decode(cursor.encode("ascii")).decode("ascii"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise HTTPException(status_code=400, detail="invalid cursor") from exc


async def _pending_request(
    repos: Repositories, session_id: UUID, request_id: str
) -> bool:
    for event in await repos.events.list_for_session(session_id):
        if (
            event.kind == EventKind.APPROVAL_REQUEST.value
            and event.payload.get("request_id") == request_id
        ):
            return True
    return False


async def _record_decision(
    repos: Repositories, session_id: UUID, request_id: str, decision: str
) -> None:
    approval = await repos.approvals.by_session_request(session_id, request_id)
    if approval is None:
        approval = orm.Approval(
            id=uuid4(),
            session_id=session_id,
            request_id=request_id,
            decision=decision,
            created_at=datetime.now(UTC),
            decided_at=datetime.now(UTC),
        )
        await repos.approvals.add(approval)
    else:
        approval.decision = decision
        approval.decided_at = datetime.now(UTC)


class _DecisionCreate(BaseModel):
    request_id: str


@router.get("/api/workspaces/{workspace_id}/sessions")
async def list_workspace_sessions(
    workspace_id: UUID, repos: Repositories = Depends(get_repositories)
) -> list[dict]:
    sessions = await repos.sessions.list(workspace_id=workspace_id)
    return [_session_payload(s) for s in sessions]


@router.get("/api/issues/{issue_id}/sessions")
async def list_issue_sessions(
    issue_id: UUID, repos: Repositories = Depends(get_repositories)
) -> list[dict]:
    sessions = await repos.sessions.list(issue_id=issue_id)
    return [_session_payload(s) for s in sessions]


@router.get("/api/sessions/{session_id}")
async def get_session(
    session_id: UUID, repos: Repositories = Depends(get_repositories)
) -> dict:
    return _session_payload(await _session_or_404(repos, session_id))


@router.get("/api/sessions/{session_id}/events")
async def get_events(
    session_id: UUID,
    from_seq: int | None = None,
    to_seq: int | None = None,
    cursor: str | None = None,
    limit: int = Query(50, ge=1, le=200),
    repos: Repositories = Depends(get_repositories),
) -> dict:
    await _session_or_404(repos, session_id)
    events = await repos.events.list_for_session(
        session_id, from_seq=from_seq, to_seq=to_seq
    )
    if cursor is not None:
        after = _decode_cursor(cursor)
        events = [e for e in events if e.sequence > after]
    page = events[:limit]
    next_cursor = _encode_cursor(page[-1].sequence) if len(events) > limit else None
    return {
        "events": [_event_payload(e) for e in page],
        "next_cursor": next_cursor,
    }


@router.post("/api/sessions/{session_id}/approve")
async def approve(
    session_id: UUID,
    body: _DecisionCreate,
    repos: Repositories = Depends(get_repositories),
) -> dict:
    await _session_or_404(repos, session_id)
    if not await _pending_request(repos, session_id, body.request_id):
        raise HTTPException(status_code=404, detail="no pending approval request")
    await _record_decision(repos, session_id, body.request_id, "approved")
    return {
        "session_id": str(session_id),
        "request_id": body.request_id,
        "decision": "approved",
    }


@router.post("/api/sessions/{session_id}/deny")
async def deny(
    session_id: UUID,
    body: _DecisionCreate,
    repos: Repositories = Depends(get_repositories),
) -> dict:
    await _session_or_404(repos, session_id)
    if not await _pending_request(repos, session_id, body.request_id):
        raise HTTPException(status_code=404, detail="no pending approval request")
    await _record_decision(repos, session_id, body.request_id, "denied")
    return {
        "session_id": str(session_id),
        "request_id": body.request_id,
        "decision": "denied",
    }


@router.post("/api/sessions/{session_id}/pause")
async def pause(
    session_id: UUID, repos: Repositories = Depends(get_repositories)
) -> dict:
    session = await _session_or_404(repos, session_id)
    if session.status != "running":
        raise HTTPException(
            status_code=409, detail=f"cannot pause session in status {session.status!r}"
        )
    session.status = "paused"
    return _session_payload(session)


@router.post("/api/sessions/{session_id}/resume")
async def resume(
    session_id: UUID, repos: Repositories = Depends(get_repositories)
) -> dict:
    session = await _session_or_404(repos, session_id)
    if session.status != "paused":
        raise HTTPException(
            status_code=409, detail=f"cannot resume session in status {session.status!r}"
        )
    session.status = "running"
    return _session_payload(session)


@router.post("/api/sessions/{session_id}/stop")
async def stop(
    session_id: UUID, repos: Repositories = Depends(get_repositories)
) -> dict:
    session = await _session_or_404(repos, session_id)
    if session.status not in {"running", "paused"}:
        raise HTTPException(
            status_code=409, detail=f"cannot stop session in status {session.status!r}"
        )
    session.status = "stopped"
    return _session_payload(session)
