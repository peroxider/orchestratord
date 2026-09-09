"""Sessions REST API (``docs/FEATURE_GAP_VS_MULTICA.md`` §5.2.3).

The execution-log / replay surface. A session is created by the backend
runner (``BackendRunner.run``), not by the Web client, so there is no create
endpoint here: the frontend lists sessions scoped to a workspace or issue and
then reads a session's ``events`` timeline, resolves ``APPROVAL_REQUEST``
events via approve/deny, and drives pause/resume/stop on the running session.

Sessions, events, and approvals persist via the repository layer (§6.1). The
SSE stream (``/events/stream``) and chat (``/messages``) endpoints are deferred
to the realtime/chat phases (§5.4 / §7.4).

Phase A.2 (§5.2.3): when a process-wide :class:`BackendRunner` is wired in
(via :func:`orchestratord.api.runtime.set_backend_runner`), the
approve/deny/pause/resume/stop endpoints forward the operator decision to
the live :class:`AgentSession` registered in ``runner.registry``. Capability
bits from the 8-bit matrix are consulted: ``approval_hooks`` gates
approve/deny, ``interrupt`` gates stop. Live-session forwarding is
opportunistic — when the daemon is in a different process (Phase B cross-
process bridge), the endpoints fall back to the Phase-1 DB-only behaviour
and the request still succeeds, so the contract tests stay green.
"""

from __future__ import annotations

import base64
import logging
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from orchestratord.api.db import get_repositories
from orchestratord.api.realtime import get_broker
from orchestratord.api.runtime import get_backend_runner
from orchestratord.db import models as orm
from orchestratord.db.repository import Repositories
from orchestratord.domain.message import Message
from orchestratord.runtime import LiveSession
from orchestratord.spi.approval import ApprovalDecision
from orchestratord.spi.events import EventKind

logger = logging.getLogger(__name__)

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


async def _publish_session_event(
    session_id: UUID, event_type: str, payload: dict[str, Any]
) -> None:
    """Fan a lifecycle event out via the realtime broker (§5.1, §5.2.3).

    Best-effort: failures here must not fail the HTTP request, because the
    DB write is the source of truth and the WebSocket subscribers can
    re-fetch via ``GET /api/sessions/{id}/events`` on reconnect.
    """
    topic = f"session.{session_id}"
    try:
        await get_broker().publish(topic, {"event": event_type, **payload})
    except Exception:
        logger.debug("broker publish failed for %s %s", topic, event_type,
                     exc_info=True)


async def _lookup_live(
    session_id: UUID, backend_runner: Any,
) -> LiveSession | None:
    """Return the live :class:`LiveSession` for ``session_id`` or ``None``.

    Centralises the ``registry.get`` lookup so the capability / forwarding
    helpers below stay readable.
    """
    if backend_runner is None:
        return None
    registry = getattr(backend_runner, "registry", None)
    if registry is None:
        return None
    return await registry.get(str(session_id))


async def _forward_to_peer(session_id: UUID, method: str, body: dict) -> None:
    """§6.2 Phase-B bridge hook: forward a decision to a reachable peer.

    Silent no-op unless an outbound peer/1 connection is open AND that
    peer advertised the matching capability in its WELCOME — Phase 1
    daemons never do, so this stays inert until Phase B turns it into
    the cross-process approval/interrupt relay. Failures never bubble
    up: the DB decision record above is the source of truth.
    """
    from orchestratord.peer.connections import live_clients

    for client in live_clients():
        if method not in client.remote_capabilities:
            continue
        try:
            await client.invoke(
                method,
                {**body, "session_id": str(session_id)},
                request_id=f"fwd-{session_id}-{method}",
            )
        except Exception:
            logger.warning(
                "peer forward of %s for session %s failed",
                method, session_id, exc_info=True,
            )


async def _forward_approval(
    session_id: UUID,
    backend_runner: Any,
    request_id: str,
    decision: ApprovalDecision,
) -> None:
    """Forward an approve/deny decision to the live SPI session (§5.2.3).

    Raises ``HTTPException(409)`` if a live session exists but its backend
    does not advertise the ``approval_hooks`` capability — in that case
    the decision is recorded in the DB but cannot be delivered, so the
    caller must be told. A missing live session is silently ignored (the
    daemon may be in a sibling process; Phase B will bridge it — the
    peer-forward hook below fires when a peer is actually reachable).
    """
    live = await _lookup_live(session_id, backend_runner)
    if live is None:
        await _forward_to_peer(
            session_id,
            "peer.approval.forward",
            {"request_id": request_id, "decision": decision.value},
        )
        return
    if not live.capabilities.approval_hooks:
        raise HTTPException(
            status_code=409,
            detail="backend does not support operator approval (approval_hooks=False)",
        )
    await live.spi_session.approve(request_id, decision)
    logger.info(
        "Approval forwarded to live session: session_id=%s request_id=%s decision=%s",
        session_id, request_id, decision.value,
    )


async def _forward_interrupt(
    session_id: UUID, backend_runner: Any,
) -> None:
    """Forward a stop to the live SPI session and its process tree (§5.2.3).

    Raises ``HTTPException(409)`` if a live session exists but lacks both
    ``interrupt`` capability AND a process tree fallback. Missing live
    session → silent no-op (Phase B cross-process bridge will route).
    """
    live = await _lookup_live(session_id, backend_runner)
    if live is None:
        await _forward_to_peer(session_id, "peer.interrupt.forward", {})
        return
    if not live.capabilities.interrupt and live.process_tree is None:
        raise HTTPException(
            status_code=409,
            detail=(
                "backend does not support operator stop "
                "(interrupt=False, no process_tree)"
            ),
        )
    if live.capabilities.interrupt:
        await live.spi_session.interrupt()
    if live.process_tree is not None:
        try:
            live.process_tree.kill()
        except Exception as exc:
            # ProcessTree raises when descendants survive SIGKILL — tell
            # the operator rather than leaking a 500.
            raise HTTPException(
                status_code=409,
                detail=f"process kill failed: {exc}",
            ) from exc
    logger.info("Stop forwarded to live session: session_id=%s", session_id)


async def _forward_process_control(
    session_id: UUID, backend_runner: Any, action: str,
) -> None:
    """Forward a pause/resume to the per-session :class:`ProcessTree`.

    Missing live session → silent no-op. Missing process tree → silent
    no-op (the SPI session may still honour the request via its own
    internal scheduling). The DB status flip is the source of truth; the
    process control call is opportunistic best-effort.
    """
    live = await _lookup_live(session_id, backend_runner)
    if live is None or live.process_tree is None:
        return
    try:
        if action == "pause":
            live.process_tree.pause()
        elif action == "resume":
            live.process_tree.resume()
    except RuntimeError as exc:
        # Per-turn backends raise this between turns (no child process to
        # signal). Raising also rolls back the DB status flip done earlier
        # in the handler, so the row keeps its pre-request status; surface
        # the race as 409 instead of leaking a 500.
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    logger.info("Process control forwarded: session_id=%s action=%s",
                session_id, action)


class _DecisionCreate(BaseModel):
    request_id: str


class _MessageCreate(BaseModel):
    """POST body for ``POST /api/sessions/{id}/messages`` (§6.1)."""

    role: str = Field(..., description="user | assistant | system | tool")
    content: str = Field(..., min_length=1)
    agent_id: UUID | None = None
    author_label: str | None = None


class _ChatStart(BaseModel):
    """POST body for ``POST /api/workspaces/{ws}/chat/sessions`` (§6.1c).

    A raw user prompt that starts a chat session without an issue. ``agent_id``
    is optional — when omitted the runner picks the workspace's default agent.
    """

    prompt: str = Field(..., min_length=1)
    agent_id: UUID | None = None


def _message_payload(message: orm.Message) -> dict:
    return {
        "id": str(message.id),
        "session_id": str(message.session_id),
        "workspace_id": str(message.workspace_id),
        "seq": message.seq,
        "role": message.role,
        "content": message.content,
        "agent_id": str(message.agent_id) if message.agent_id else None,
        "author_label": message.author_label,
        "created_at": message.created_at.isoformat(),
    }


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
    backend_runner: Any = Depends(get_backend_runner),
) -> dict:
    await _session_or_404(repos, session_id)
    if not await _pending_request(repos, session_id, body.request_id):
        raise HTTPException(status_code=404, detail="no pending approval request")
    await _record_decision(repos, session_id, body.request_id, "approved")
    await _forward_approval(
        session_id, backend_runner, body.request_id, ApprovalDecision.ALLOW,
    )
    await _publish_session_event(
        session_id, "approval_resolved",
        {"request_id": body.request_id, "decision": "approved"},
    )
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
    backend_runner: Any = Depends(get_backend_runner),
) -> dict:
    await _session_or_404(repos, session_id)
    if not await _pending_request(repos, session_id, body.request_id):
        raise HTTPException(status_code=404, detail="no pending approval request")
    await _record_decision(repos, session_id, body.request_id, "denied")
    await _forward_approval(
        session_id, backend_runner, body.request_id, ApprovalDecision.DENY,
    )
    await _publish_session_event(
        session_id, "approval_resolved",
        {"request_id": body.request_id, "decision": "denied"},
    )
    return {
        "session_id": str(session_id),
        "request_id": body.request_id,
        "decision": "denied",
    }


@router.post("/api/sessions/{session_id}/pause")
async def pause(
    session_id: UUID,
    repos: Repositories = Depends(get_repositories),
    backend_runner: Any = Depends(get_backend_runner),
) -> dict:
    session = await _session_or_404(repos, session_id)
    if session.status != "running":
        raise HTTPException(
            status_code=409, detail=f"cannot pause session in status {session.status!r}"
        )
    session.status = "paused"
    await _forward_process_control(session_id, backend_runner, "pause")
    await _publish_session_event(session_id, "status_changed", {"status": "paused"})
    return _session_payload(session)


@router.post("/api/sessions/{session_id}/resume")
async def resume(
    session_id: UUID,
    repos: Repositories = Depends(get_repositories),
    backend_runner: Any = Depends(get_backend_runner),
) -> dict:
    session = await _session_or_404(repos, session_id)
    if session.status != "paused":
        raise HTTPException(
            status_code=409, detail=f"cannot resume session in status {session.status!r}"
        )
    session.status = "running"
    await _forward_process_control(session_id, backend_runner, "resume")
    await _publish_session_event(session_id, "status_changed", {"status": "running"})
    return _session_payload(session)


@router.post("/api/sessions/{session_id}/stop")
async def stop(
    session_id: UUID,
    repos: Repositories = Depends(get_repositories),
    backend_runner: Any = Depends(get_backend_runner),
) -> dict:
    session = await _session_or_404(repos, session_id)
    if session.status not in {"running", "paused"}:
        raise HTTPException(
            status_code=409, detail=f"cannot stop session in status {session.status!r}"
        )
    session.status = "stopped"
    await _forward_interrupt(session_id, backend_runner)
    await _publish_session_event(session_id, "status_changed", {"status": "stopped"})
    return _session_payload(session)


# ---------------------------------------------------------------------------
# Chat session start (§6.1c)
# ---------------------------------------------------------------------------


async def _workspace_or_404(
    repos: Repositories, workspace_id: UUID
) -> orm.Workspace:
    workspace = await repos.workspaces.get(workspace_id)
    if workspace is None:
        raise HTTPException(status_code=404, detail="workspace not found")
    return workspace


@router.post(
    "/api/workspaces/{workspace_id}/chat/sessions", status_code=201
)
async def start_chat_session(
    workspace_id: UUID,
    body: _ChatStart,
    repos: Repositories = Depends(get_repositories),
) -> dict:
    """Start a chat session from a raw prompt (§6.1c).

    Creates ``Session(issue_id=NULL, mode="single", status="pending")`` plus
    the initial ``user`` ``Message`` row, then returns both. BackendRunner
    triggering is intentionally NOT done here — the chat daemon (Phase
    B §6.1d, deferred) polls ``status="pending"`` sessions with no live
    runner and dispatches them. This keeps the endpoint deterministic: same
    in single-process and cross-process deployment (cf. §5.2.3 note about
    live-session forwarding being opportunistic).
    """
    await _workspace_or_404(repos, workspace_id)
    if body.agent_id is not None:
        agent = await repos.agents.get(body.agent_id)
        if agent is None or agent.workspace_id != workspace_id:
            raise HTTPException(
                status_code=404, detail="agent not found in workspace"
            )

    session_id = uuid4()
    session = orm.Session(
        id=session_id,
        workspace_id=workspace_id,
        issue_id=None,
        agent_id=body.agent_id,
        run_id=None,
        mode="single",
        status="pending",
        created_at=datetime.now(UTC),
    )
    await repos.sessions.add(session)

    initial_message = orm.Message(
        id=uuid4(),
        session_id=session_id,
        workspace_id=workspace_id,
        seq=0,  # sentinel — repository auto-assigns the next seq (0 here)
        role="user",
        content=body.prompt,
        agent_id=None,
        author_label="me",
        created_at=datetime.now(UTC),
    )
    await repos.messages.append(initial_message)

    await _publish_session_event(
        session_id, "chat_started",
        {"prompt": body.prompt, "agent_id": str(body.agent_id) if body.agent_id else None},
    )

    return {
        "session_id": str(session_id),
        "workspace_id": str(workspace_id),
        "status": session.status,
        "message": _message_payload(initial_message),
    }


# ---------------------------------------------------------------------------
# Messages (chat timeline, §6.1)
# ---------------------------------------------------------------------------


@router.get("/api/sessions/{session_id}/messages")
async def list_session_messages(
    session_id: UUID,
    after_seq: int | None = Query(None, ge=0),
    limit: int = Query(100, ge=1, le=500),
    repos: Repositories = Depends(get_repositories),
) -> dict:
    """Return the chat timeline for a session, ordered by ``seq`` ascending.

    The chat UI fetches this on mount and re-fetches with ``after_seq`` on
    each new turn to avoid re-downloading the whole history. The endpoint is
    intentionally read-only — message creation lives below so the same body
    shape can be used for both user posts and runner-injected assistant
    turns (Phase B §6.1c).
    """
    session = await _session_or_404(repos, session_id)
    messages = await repos.messages.list_for_session(
        session_id, after_seq=after_seq, limit=limit
    )
    return {
        "session_id": str(session.id),
        "workspace_id": str(session.workspace_id),
        "messages": [_message_payload(m) for m in messages],
    }


@router.post("/api/sessions/{session_id}/messages", status_code=201)
async def post_session_message(
    session_id: UUID,
    body: _MessageCreate,
    repos: Repositories = Depends(get_repositories),
) -> dict:
    """Append a chat message to the session timeline (§6.1).

    Per-session ``seq`` is assigned atomically by the repository
    (``COALESCE(MAX(seq), -1) + 1``) so concurrent user posts and
    runner-injected turns can't interleave. The endpoint does NOT trigger
    the BackendRunner — that's the responsibility of
    ``POST /api/workspaces/{ws}/chat/sessions`` (Phase B §6.1c, deferred to
    the chat-flow increment); this endpoint is the persistence surface.
    """
    session = await _session_or_404(repos, session_id)
    try:
        validated = Message(
            id=uuid4(),
            session_id=session.id,
            workspace_id=session.workspace_id,
            seq=0,  # sentinel — repository auto-assigns the next seq
            role=body.role,
            content=body.content,
            agent_id=body.agent_id,
            author_label=body.author_label,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    row = orm.Message(
        id=validated.id,
        session_id=validated.session_id,
        workspace_id=validated.workspace_id,
        seq=validated.seq,
        role=validated.role,
        content=validated.content,
        agent_id=validated.agent_id,
        author_label=validated.author_label,
        created_at=validated.created_at,
    )
    await repos.messages.append(row)
    return _message_payload(row)

