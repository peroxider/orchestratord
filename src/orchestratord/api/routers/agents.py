"""Agents REST API (``docs/FEATURE_GAP_VS_MULTICA.md`` §5.2.2).

Bridges the domain ``Agent`` entity to the Web surface, persisted via the
``agents`` table (§6.2). ``capabilities`` returns the cached capability bits
verbatim so the frontend capability matrix never re-derives them; ``doctor``
runs the backend's preflight through ``resolve_backend`` so a missing or
misconfigured provider surfaces here rather than mid-run.
"""

from __future__ import annotations

import os
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from orchestratord.api.db import get_repositories
from orchestratord.db import models as orm
from orchestratord.db.repository import Repositories
from orchestratord.domain.agent import Agent

router = APIRouter(tags=["agents"])


def _agent_payload(agent: orm.Agent) -> dict:
    return {
        "id": str(agent.id),
        "workspace_id": str(agent.workspace_id),
        "name": agent.name,
        "provider": agent.provider,
        "runtime_id": str(agent.runtime_id),
        "capabilities_cache_jsonb": dict(agent.capabilities_cache_jsonb),
        "created_at": agent.created_at.isoformat(),
    }


async def _agent_or_404(repos: Repositories, agent_id: UUID) -> orm.Agent:
    agent = await repos.agents.get(agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail="agent not found")
    return agent


class _AgentCreate(BaseModel):
    name: str
    provider: str
    runtime_id: UUID
    capabilities_cache_jsonb: dict


@router.get("/api/workspaces/{workspace_id}/agents")
async def list_workspace_agents(
    workspace_id: UUID, repos: Repositories = Depends(get_repositories)
) -> list[dict]:
    agents = await repos.agents.list_for_workspace(workspace_id)
    return [_agent_payload(a) for a in agents]


@router.post("/api/workspaces/{workspace_id}/agents", status_code=201)
async def create_agent(
    workspace_id: UUID,
    body: _AgentCreate,
    repos: Repositories = Depends(get_repositories),
) -> dict:
    try:
        validated = Agent(
            id=uuid4(),
            workspace_id=workspace_id,
            name=body.name.strip(),
            provider=body.provider,
            runtime_id=body.runtime_id,
            capabilities_cache_jsonb=body.capabilities_cache_jsonb,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    agent = orm.Agent(
        id=validated.id,
        workspace_id=validated.workspace_id,
        name=validated.name,
        provider=validated.provider,
        runtime_id=validated.runtime_id,
        capabilities_cache_jsonb=validated.capabilities_cache_jsonb,
        created_at=validated.created_at,
    )
    await repos.agents.add(agent)
    return _agent_payload(agent)


@router.get("/api/agents/{agent_id}/capabilities")
async def get_capabilities(
    agent_id: UUID, repos: Repositories = Depends(get_repositories)
) -> dict:
    agent = await _agent_or_404(repos, agent_id)
    return dict(agent.capabilities_cache_jsonb)


@router.post("/api/agents/{agent_id}/doctor")
async def doctor(
    agent_id: UUID, repos: Repositories = Depends(get_repositories)
) -> dict:
    agent = await _agent_or_404(repos, agent_id)
    detail: str | None = None
    try:
        from orchestratord.backend_registry import resolve_backend
        from orchestratord.spi.backend import SessionSpec

        backend = resolve_backend(agent.provider, strict=True)
        backend.preflight(SessionSpec(cwd=os.getcwd(), provider=agent.provider))
    except Exception as exc:  # noqa: BLE001 - doctor must report, not crash
        detail = str(exc)
    return {
        "agent_id": str(agent_id),
        "ready": detail is None,
        "detail": detail or "ok",
    }
