"""Squads REST API (``docs/FEATURE_GAP_VS_MULTICA.md`` §7.1).

Squads group members (humans or agents) under a leader that routes work.
Phase 1 serves create/list/detail/soft-delete over the persistence layer, plus
the two routing verbs the doc specifies: ``assign`` (leader manually hands an
issue to a member) and ``route`` (auto-route an issue to the first non-leader
candidate). Every workspace-scoped read re-checks ``workspace_id`` so
cross-workspace access is rejected (§6.1 / multica rule). Routing itself is a
Phase-5 workflow concern; here assign/route validate the request and ack,
leaving the actual issue dispatch to the workflow engine.

Leader/member polymorphism and self-leader prevention are enforced by the
domain ``Squad``; the goal-mode capability gate for agent leaders is enforced
here (via ``repos.agents.get``) since it depends on the persistence layer.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from orchestratord.api.db import get_repositories
from orchestratord.db import models as orm
from orchestratord.db.repository import Repositories
from orchestratord.domain.squad import Squad, SquadMember

router = APIRouter(tags=["squads"])


async def _squad_payload(repos: Repositories, squad: orm.Squad) -> dict:
    members = await repos.squad_members.list_for_squad(squad.id)
    return {
        "id": str(squad.id),
        "workspace_id": str(squad.workspace_id),
        "name": squad.name,
        "leader_type": squad.leader_type,
        "leader_id": str(squad.leader_id),
        "members": [
            {"member_type": m.member_type, "member_id": str(m.member_id)}
            for m in members
        ],
        "created_at": squad.created_at.isoformat() if squad.created_at else None,
    }


async def _squad_or_404(
    repos: Repositories, workspace_id: UUID, squad_id: UUID
) -> orm.Squad:
    squad = await repos.squads.get(squad_id)
    if squad is None or squad.is_deleted or squad.workspace_id != workspace_id:
        raise HTTPException(status_code=404, detail="squad not found")
    return squad


async def _squad_by_id_or_404(repos: Repositories, squad_id: UUID) -> orm.Squad:
    squad = await repos.squads.get(squad_id)
    if squad is None or squad.is_deleted:
        raise HTTPException(status_code=404, detail="squad not found")
    return squad


class _MemberIn(BaseModel):
    member_type: str
    member_id: UUID


class _SquadCreate(BaseModel):
    name: str
    leader_type: str
    leader_id: UUID
    members: list[_MemberIn] = []


class _AssignIn(BaseModel):
    issue_id: UUID
    member_type: str
    member_id: UUID


class _RouteIn(BaseModel):
    issue_id: UUID


@router.get("/api/workspaces/{workspace_id}/squads")
async def list_squads(
    workspace_id: UUID, repos: Repositories = Depends(get_repositories)
) -> list[dict]:
    squads = await repos.squads.list_for_workspace(workspace_id)
    squads = [s for s in squads if not s.is_deleted]
    return [await _squad_payload(repos, s) for s in squads]


@router.post("/api/workspaces/{workspace_id}/squads", status_code=201)
async def create_squad(
    workspace_id: UUID,
    body: _SquadCreate,
    repos: Repositories = Depends(get_repositories),
) -> dict:
    try:
        validated = Squad(
            id=uuid4(),
            workspace_id=workspace_id,
            name=body.name.strip(),
            leader_type=body.leader_type,
            leader_id=body.leader_id,
            members=[
                SquadMember(m.member_type, m.member_id) for m in body.members
            ],
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if validated.leader_type == "agent":
        leader = await repos.agents.get(validated.leader_id)
        if leader is None:
            raise HTTPException(status_code=404, detail="agent leader not found")
        if leader.workspace_id != workspace_id:
            raise HTTPException(status_code=404, detail="agent leader not in workspace")
        if not leader.capabilities_cache_jsonb.get("goal_mode"):
            raise HTTPException(
                status_code=422,
                detail="agent leader lacks goal_mode capability; cannot route work",
            )
    squad = orm.Squad(
        id=validated.id,
        workspace_id=workspace_id,
        name=validated.name,
        leader_type=validated.leader_type,
        leader_id=validated.leader_id,
        is_deleted=False,
        created_at=datetime.now(UTC),
    )
    await repos.squads.add(squad)
    for m in validated.members:
        await repos.squad_members.add(
            orm.SquadMember(
                squad_id=squad.id, member_type=m.member_type, member_id=m.member_id
            )
        )
    return await _squad_payload(repos, squad)


@router.get("/api/workspaces/{workspace_id}/squads/{squad_id}")
async def get_squad(
    workspace_id: UUID,
    squad_id: UUID,
    repos: Repositories = Depends(get_repositories),
) -> dict:
    squad = await _squad_or_404(repos, workspace_id, squad_id)
    return await _squad_payload(repos, squad)


@router.delete("/api/workspaces/{workspace_id}/squads/{squad_id}", status_code=204)
async def delete_squad(
    workspace_id: UUID,
    squad_id: UUID,
    repos: Repositories = Depends(get_repositories),
) -> None:
    squad = await _squad_or_404(repos, workspace_id, squad_id)
    squad.is_deleted = True


@router.post("/api/squads/{squad_id}/assign", status_code=202)
async def assign(
    squad_id: UUID,
    body: _AssignIn,
    repos: Repositories = Depends(get_repositories),
) -> dict:
    await _squad_by_id_or_404(repos, squad_id)
    members = await repos.squad_members.list_for_squad(squad_id)
    if not any(
        m.member_type == body.member_type and m.member_id == body.member_id
        for m in members
    ):
        raise HTTPException(status_code=404, detail="member not in squad")
    return {
        "assigned": True,
        "squad_id": str(squad_id),
        "issue_id": str(body.issue_id),
        "member_type": body.member_type,
        "member_id": str(body.member_id),
    }


@router.post("/api/squads/{squad_id}/route", status_code=202)
async def route(
    squad_id: UUID,
    body: _RouteIn,
    repos: Repositories = Depends(get_repositories),
) -> dict:
    squad = await _squad_by_id_or_404(repos, squad_id)
    members = await repos.squad_members.list_for_squad(squad_id)
    target = next(
        (
            m
            for m in members
            if (m.member_id, m.member_type) != (squad.leader_id, squad.leader_type)
        ),
        None,
    )
    if target is None:
        raise HTTPException(status_code=422, detail="squad has no routable members")
    return {
        "routed": True,
        "squad_id": str(squad_id),
        "issue_id": str(body.issue_id),
        "member_type": target.member_type,
        "member_id": str(target.member_id),
    }
