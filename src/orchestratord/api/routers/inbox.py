"""Inbox REST API (``docs/FEATURE_GAP_VS_MULTICA.md`` §5.2.6).

The human-intervention surface. The event worker posts an inbox item when an
APPROVAL_REQUEST / clarification / failed event needs a person (§5.2.6); the
Web client lists workspace items and resolves / assigns / dismisses them.
State transitions are enforced by the domain :class:`InboxItem`; the WebSocket
push (``inbox.created`` / ``inbox.resolved``) is the realtime concern (§6.4),
not exercised here.
"""

from __future__ import annotations

from typing import Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from orchestratord.api.db import get_repositories
from orchestratord.db import models as orm
from orchestratord.db.repository import Repositories
from orchestratord.domain.inbox import InboxItem

router = APIRouter(tags=["inbox"])


def _inbox_payload(item: orm.InboxItem) -> dict:
    return {
        "id": str(item.id),
        "workspace_id": str(item.workspace_id),
        "kind": item.kind,
        "title": item.title,
        "issue_id": str(item.issue_id) if item.issue_id else None,
        "session_id": str(item.session_id) if item.session_id else None,
        "event_seq": item.event_seq,
        "status": item.status,
        "assignee_type": item.assignee_type,
        "assignee_id": str(item.assignee_id) if item.assignee_id else None,
        "created_at": item.created_at.isoformat(),
    }


def _to_domain(item: orm.InboxItem) -> InboxItem:
    """Project an ORM row onto the domain entity to reuse its state machine."""
    return InboxItem(
        id=item.id,
        workspace_id=item.workspace_id,
        kind=item.kind,
        title=item.title,
        issue_id=item.issue_id,
        session_id=item.session_id,
        event_seq=item.event_seq,
        status=item.status,
        assignee_type=item.assignee_type,
        assignee_id=item.assignee_id,
        created_at=item.created_at,
    )


async def _inbox_or_404(
    repos: Repositories, workspace_id: UUID, item_id: UUID
) -> orm.InboxItem:
    item = await repos.inbox.get(item_id)
    if item is None or item.workspace_id != workspace_id:
        raise HTTPException(status_code=404, detail="inbox item not found")
    return item


class _InboxCreate(BaseModel):
    kind: str
    title: str
    issue_id: UUID | None = None
    session_id: UUID | None = None
    event_seq: int | None = None


class _AssignIn(BaseModel):
    assignee_type: Literal["member", "agent"]
    assignee_id: UUID


@router.get("/api/workspaces/{workspace_id}/inbox")
async def list_inbox(
    workspace_id: UUID,
    status: str | None = Query(None),
    repos: Repositories = Depends(get_repositories),
) -> list[dict]:
    items = await repos.inbox.list_for_workspace(workspace_id)
    if status is not None:
        items = [i for i in items if i.status == status]
    return [_inbox_payload(i) for i in items]


@router.post("/api/workspaces/{workspace_id}/inbox", status_code=201)
async def create_inbox_item(
    workspace_id: UUID,
    body: _InboxCreate,
    repos: Repositories = Depends(get_repositories),
) -> dict:
    try:
        validated = InboxItem(
            id=uuid4(),
            workspace_id=workspace_id,
            kind=body.kind,
            title=body.title,
            issue_id=body.issue_id,
            session_id=body.session_id,
            event_seq=body.event_seq,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    item = orm.InboxItem(
        id=validated.id,
        workspace_id=workspace_id,
        kind=validated.kind,
        title=validated.title,
        issue_id=validated.issue_id,
        session_id=validated.session_id,
        event_seq=validated.event_seq,
        status=validated.status,
        assignee_type=validated.assignee_type,
        assignee_id=validated.assignee_id,
        created_at=validated.created_at,
    )
    await repos.inbox.add(item)
    return _inbox_payload(item)


@router.get("/api/workspaces/{workspace_id}/inbox/{item_id}")
async def get_inbox_item(
    workspace_id: UUID,
    item_id: UUID,
    repos: Repositories = Depends(get_repositories),
) -> dict:
    return _inbox_payload(await _inbox_or_404(repos, workspace_id, item_id))


@router.post("/api/workspaces/{workspace_id}/inbox/{item_id}/assign")
async def assign(
    workspace_id: UUID,
    item_id: UUID,
    body: _AssignIn,
    repos: Repositories = Depends(get_repositories),
) -> dict:
    item = await _inbox_or_404(repos, workspace_id, item_id)
    domain = _to_domain(item)
    try:
        domain.assign(body.assignee_type, body.assignee_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    item.assignee_type = domain.assignee_type
    item.assignee_id = domain.assignee_id
    item.status = domain.status
    return _inbox_payload(item)


@router.post("/api/workspaces/{workspace_id}/inbox/{item_id}/resolve")
async def resolve(
    workspace_id: UUID,
    item_id: UUID,
    repos: Repositories = Depends(get_repositories),
) -> dict:
    item = await _inbox_or_404(repos, workspace_id, item_id)
    domain = _to_domain(item)
    try:
        domain.resolve()
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    item.status = domain.status
    return _inbox_payload(item)


@router.post("/api/workspaces/{workspace_id}/inbox/{item_id}/dismiss")
async def dismiss(
    workspace_id: UUID,
    item_id: UUID,
    repos: Repositories = Depends(get_repositories),
) -> dict:
    item = await _inbox_or_404(repos, workspace_id, item_id)
    domain = _to_domain(item)
    try:
        domain.dismiss()
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    item.status = domain.status
    return _inbox_payload(item)
