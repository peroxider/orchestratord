"""Autopilots REST API (``docs/FEATURE_GAP_VS_MULTICA.md`` §7.3).

An autopilot is a cron-like periodic task that launches a declarative
workflow. Phase 1 serves workspace-scoped CRUD over the ``autopilots`` table
plus a ``run`` history sub-resource over ``autopilot_runs``. Scheduling itself
is the Phase-5 apscheduler/asyncio-loop concern; here the router records runs
and exposes the history the Web client lists.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from orchestratord.api.db import get_repositories
from orchestratord.db import models as orm
from orchestratord.db.repository import Repositories

router = APIRouter(tags=["autopilots"])


def _autopilot_payload(autopilot: orm.Autopilot) -> dict:
    return {
        "id": str(autopilot.id),
        "workspace_id": str(autopilot.workspace_id),
        "name": autopilot.name,
        "cron": autopilot.cron,
        "prompt": autopilot.prompt,
        "target_kind": autopilot.target_kind,
        "target_id": str(autopilot.target_id),
        "enabled": autopilot.enabled,
    }


def _run_payload(run: orm.AutopilotRun) -> dict:
    return {
        "autopilot_id": str(run.autopilot_id),
        "scheduled_at": run.scheduled_at.isoformat(),
        "run_id": str(run.run_id),
        "status": run.status,
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
    }


async def _autopilot_or_404(
    repos: Repositories, workspace_id: UUID, autopilot_id: UUID
) -> orm.Autopilot:
    autopilot = await repos.autopilots.get(autopilot_id)
    if autopilot is None or autopilot.workspace_id != workspace_id:
        raise HTTPException(status_code=404, detail="autopilot not found")
    return autopilot


class _AutopilotCreate(BaseModel):
    name: str
    cron: str
    prompt: str
    target_kind: str
    target_id: UUID
    enabled: bool = True


class _AutopilotPatch(BaseModel):
    enabled: bool


class _RunCreate(BaseModel):
    scheduled_at: datetime
    run_id: UUID
    status: str
    started_at: datetime | None = None
    finished_at: datetime | None = None


@router.get("/api/workspaces/{workspace_id}/autopilots")
async def list_autopilots(
    workspace_id: UUID, repos: Repositories = Depends(get_repositories)
) -> list[dict]:
    autopilots = await repos.autopilots.list_for_workspace(workspace_id)
    return [_autopilot_payload(a) for a in autopilots]


@router.post("/api/workspaces/{workspace_id}/autopilots", status_code=201)
async def create_autopilot(
    workspace_id: UUID,
    body: _AutopilotCreate,
    repos: Repositories = Depends(get_repositories),
) -> dict:
    autopilot = orm.Autopilot(
        id=uuid4(),
        workspace_id=workspace_id,
        name=body.name.strip(),
        cron=body.cron,
        prompt=body.prompt,
        target_kind=body.target_kind,
        target_id=body.target_id,
        enabled=body.enabled,
    )
    await repos.autopilots.add(autopilot)
    return _autopilot_payload(autopilot)


@router.get("/api/workspaces/{workspace_id}/autopilots/{autopilot_id}")
async def get_autopilot(
    workspace_id: UUID,
    autopilot_id: UUID,
    repos: Repositories = Depends(get_repositories),
) -> dict:
    autopilot = await _autopilot_or_404(repos, workspace_id, autopilot_id)
    payload = _autopilot_payload(autopilot)
    runs = await repos.autopilot_runs.list_for_autopilot(autopilot_id)
    payload["runs"] = [_run_payload(r) for r in runs]
    return payload


@router.patch("/api/workspaces/{workspace_id}/autopilots/{autopilot_id}")
async def patch_autopilot(
    workspace_id: UUID,
    autopilot_id: UUID,
    body: _AutopilotPatch,
    repos: Repositories = Depends(get_repositories),
) -> dict:
    autopilot = await _autopilot_or_404(repos, workspace_id, autopilot_id)
    autopilot.enabled = body.enabled
    return _autopilot_payload(autopilot)


@router.post(
    "/api/workspaces/{workspace_id}/autopilots/{autopilot_id}/runs",
    status_code=201,
)
async def record_run(
    workspace_id: UUID,
    autopilot_id: UUID,
    body: _RunCreate,
    repos: Repositories = Depends(get_repositories),
) -> dict:
    await _autopilot_or_404(repos, workspace_id, autopilot_id)
    run = orm.AutopilotRun(
        id=uuid4(),
        autopilot_id=autopilot_id,
        scheduled_at=body.scheduled_at,
        run_id=body.run_id,
        status=body.status,
        started_at=body.started_at,
        finished_at=body.finished_at,
    )
    await repos.autopilot_runs.add(run)
    return _run_payload(run)
