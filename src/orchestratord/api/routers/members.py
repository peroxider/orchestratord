"""Members REST API (``docs/FEATURE_GAP_VS_MULTICA.md`` §5.7.1).

Multi-tenancy membership surface: a workspace owns members carrying one of the
three roles (owner / admin / member); ``member_agent_scopes`` is the
many-to-many join granting a member access to specific agents. Role is
validated at the API boundary (``Literal``) and again at the entity layer.

Reference: docs/FEATURE_GAP_VS_MULTICA.md §5.7.1, §6.1.
"""

from __future__ import annotations

from typing import Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from orchestratord.api.db import get_repositories
from orchestratord.db import models as orm
from orchestratord.db.repository import Repositories
from orchestratord.domain.workspace import Member

router = APIRouter(tags=["members"])


def _member_payload(member: orm.Member) -> dict:
    return {
        "id": str(member.id),
        "workspace_id": str(member.workspace_id),
        "role": member.role,
        "name": member.name,
        "created_at": member.created_at.isoformat() if member.created_at else None,
    }


async def _member_or_404(
    repos: Repositories, workspace_id: UUID, member_id: UUID
) -> orm.Member:
    member = await repos.members.get(member_id)
    if member is None or member.workspace_id != workspace_id:
        raise HTTPException(status_code=404, detail="member not found")
    return member


async def _scope_exists(
    repos: Repositories, member_id: UUID, agent_id: UUID
) -> bool:
    scopes = await repos.member_agent_scopes.list_for_member(member_id)
    return any(s.agent_id == agent_id for s in scopes)


class _MemberCreate(BaseModel):
    role: Literal["owner", "admin", "member"]
    name: str = ""


class _MemberUpdate(BaseModel):
    role: Literal["owner", "admin", "member"] | None = None
    name: str | None = None


class _ScopeGrant(BaseModel):
    agent_id: UUID


@router.get("/api/workspaces/{workspace_id}/members")
async def list_members(
    workspace_id: UUID, repos: Repositories = Depends(get_repositories)
) -> list[dict]:
    members = await repos.members.list_for_workspace(workspace_id)
    return [_member_payload(m) for m in members]


@router.post("/api/workspaces/{workspace_id}/members", status_code=201)
async def create_member(
    workspace_id: UUID,
    body: _MemberCreate,
    repos: Repositories = Depends(get_repositories),
) -> dict:
    try:
        validated = Member(
            id=uuid4(),
            workspace_id=workspace_id,
            role=body.role,
            name=body.name.strip(),
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    member = orm.Member(
        id=validated.id,
        workspace_id=workspace_id,
        role=validated.role,
        name=validated.name,
        created_at=validated.created_at,
    )
    await repos.members.add(member)
    return _member_payload(member)


@router.get("/api/workspaces/{workspace_id}/members/{member_id}")
async def get_member(
    workspace_id: UUID,
    member_id: UUID,
    repos: Repositories = Depends(get_repositories),
) -> dict:
    return _member_payload(await _member_or_404(repos, workspace_id, member_id))


@router.patch("/api/workspaces/{workspace_id}/members/{member_id}")
async def update_member(
    workspace_id: UUID,
    member_id: UUID,
    body: _MemberUpdate,
    repos: Repositories = Depends(get_repositories),
) -> dict:
    member = await _member_or_404(repos, workspace_id, member_id)
    if body.role is not None:
        member.role = body.role
    if body.name is not None:
        member.name = body.name.strip()
    return _member_payload(member)


@router.delete("/api/workspaces/{workspace_id}/members/{member_id}", status_code=204)
async def delete_member(
    workspace_id: UUID,
    member_id: UUID,
    repos: Repositories = Depends(get_repositories),
) -> None:
    member = await _member_or_404(repos, workspace_id, member_id)
    for scope in await repos.member_agent_scopes.list_for_member(member_id):
        await repos.member_agent_scopes.delete(scope)
    await repos.members.delete(member)


@router.post("/api/workspaces/{workspace_id}/members/{member_id}/scopes")
async def grant_scope(
    workspace_id: UUID,
    member_id: UUID,
    body: _ScopeGrant,
    repos: Repositories = Depends(get_repositories),
) -> dict:
    await _member_or_404(repos, workspace_id, member_id)
    if not await _scope_exists(repos, member_id, body.agent_id):
        await repos.member_agent_scopes.add(
            orm.MemberAgentScope(member_id=member_id, agent_id=body.agent_id)
        )
    return {"member_id": str(member_id), "agent_id": str(body.agent_id)}


@router.get("/api/workspaces/{workspace_id}/members/{member_id}/scopes")
async def list_scopes(
    workspace_id: UUID,
    member_id: UUID,
    repos: Repositories = Depends(get_repositories),
) -> dict:
    await _member_or_404(repos, workspace_id, member_id)
    scopes = await repos.member_agent_scopes.list_for_member(member_id)
    agent_ids = sorted(str(s.agent_id) for s in scopes)
    return {"member_id": str(member_id), "agent_ids": agent_ids}


@router.delete(
    "/api/workspaces/{workspace_id}/members/{member_id}/scopes/{agent_id}",
    status_code=204,
)
async def revoke_scope(
    workspace_id: UUID,
    member_id: UUID,
    agent_id: UUID,
    repos: Repositories = Depends(get_repositories),
) -> None:
    await _member_or_404(repos, workspace_id, member_id)
    for scope in await repos.member_agent_scopes.list_for_member(member_id):
        if scope.agent_id == agent_id:
            await repos.member_agent_scopes.delete(scope)
