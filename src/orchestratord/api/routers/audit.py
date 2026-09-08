"""Audit log REST API (``docs/FEATURE_GAP_VS_MULTICA.md`` §5.7.3).

Every Web-triggered mutation (create issue / reassign / approve) writes an
``audit_log`` row. This router exposes the append-and-read surface: the POST
models that write (mirroring how ``usage`` ingests worker records), while the
GET powers the admin audit page's filtering (actor_type / target_type /
action / time window) and JSON export. Entries are immutable and returned
newest-first, matching the §6.1.2 ``(workspace_id, created_at desc)`` index.

Reference: docs/FEATURE_GAP_VS_MULTICA.md §5.7.3, §6.1.1, §6.1.2.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from orchestratord.api.db import get_repositories
from orchestratord.db import models as orm
from orchestratord.db.repository import Repositories
from orchestratord.domain.audit import AuditLogEntry

router = APIRouter(tags=["audit"])


def _audit_payload(entry: orm.AuditLogEntry) -> dict:
    return {
        "id": str(entry.id),
        "workspace_id": str(entry.workspace_id),
        "actor_type": entry.actor_type,
        "actor_id": entry.actor_id,
        "action": entry.action,
        "target_type": entry.target_type,
        "target_id": entry.target_id,
        "payload_jsonb": entry.payload_jsonb,
        "created_at": entry.created_at.isoformat(),
    }


def _filter_window(
    entries: list[orm.AuditLogEntry],
    from_: datetime | None,
    to: datetime | None,
) -> list[orm.AuditLogEntry]:
    if from_ is not None:
        if from_.tzinfo is None:
            from_ = from_.replace(tzinfo=UTC)
        entries = [e for e in entries if e.created_at >= from_]
    if to is not None:
        if to.tzinfo is None:
            to = to.replace(tzinfo=UTC)
        entries = [e for e in entries if e.created_at <= to]
    return entries


class _AuditCreate(BaseModel):
    actor_type: Literal["member", "agent", "system"]
    actor_id: UUID | str
    action: str
    target_type: str
    target_id: UUID | str
    payload_jsonb: dict | None = None


@router.post("/api/workspaces/{workspace_id}/audit", status_code=201)
async def append_audit(
    workspace_id: UUID,
    body: _AuditCreate,
    repos: Repositories = Depends(get_repositories),
) -> dict:
    try:
        validated = AuditLogEntry(
            id=uuid4(),
            workspace_id=workspace_id,
            actor_type=body.actor_type,
            actor_id=body.actor_id,
            action=body.action,
            target_type=body.target_type,
            target_id=body.target_id,
            payload_jsonb=body.payload_jsonb,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    entry = orm.AuditLogEntry(
        id=validated.id,
        workspace_id=workspace_id,
        actor_type=validated.actor_type,
        actor_id=str(validated.actor_id),
        action=validated.action,
        target_type=validated.target_type,
        target_id=str(validated.target_id),
        payload_jsonb=validated.payload_jsonb,
        created_at=validated.created_at,
    )
    await repos.audit_log.add(entry)
    return _audit_payload(entry)


@router.get("/api/workspaces/{workspace_id}/audit")
async def list_audit(
    workspace_id: UUID,
    actor_type: Literal["member", "agent", "system"] | None = Query(None),
    target_type: str | None = Query(None),
    action: str | None = Query(None),
    from_: Annotated[datetime | None, Query(alias="from")] = None,
    to: Annotated[datetime | None, Query()] = None,
    repos: Repositories = Depends(get_repositories),
) -> list[dict]:
    entries = await repos.audit_log.list_for_workspace(workspace_id)
    if actor_type is not None:
        entries = [e for e in entries if e.actor_type == actor_type]
    if target_type is not None:
        entries = [e for e in entries if e.target_type == target_type]
    if action is not None:
        entries = [e for e in entries if e.action == action]
    entries = _filter_window(entries, from_, to)
    return [_audit_payload(e) for e in entries]
