"""Runtimes REST API (``docs/FEATURE_GAP_VS_MULTICA.md`` §5.2.7, §6.3).

A runtime is a host machine that has run ``orchestratord daemon start
--workspace-token ...`` and connects over WebSocket. The server issues a
one-time token on registration; only the token hash is stored on the entity
(plaintext never persists). The Web client renders a runtime card (hostname /
OS / probed backends / last heartbeat), so list/detail never expose the
token. Heartbeat and backend-probe reporting mirror the daemon uplink;
``revoke`` flips the runtime to DISABLED, which also closes its WS.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from orchestratord.api.db import get_repositories
from orchestratord.db import models as orm
from orchestratord.db.repository import Repositories
from orchestratord.domain.runtime import RuntimeStatus, issue_runtime_token

router = APIRouter(tags=["runtimes"])


async def _runtime_payload(repos: Repositories, runtime: orm.Runtime) -> dict:
    backends = await repos.runtime_backends.list_for_runtime(runtime.id)
    return {
        "id": str(runtime.id),
        "workspace_id": str(runtime.workspace_id),
        "hostname": runtime.hostname,
        "os": runtime.os,
        "status": runtime.status,
        "last_seen_at": (
            runtime.last_seen_at.isoformat() if runtime.last_seen_at else None
        ),
        "created_at": (
            runtime.created_at.isoformat() if runtime.created_at else None
        ),
        "probed_backends": [
            {"name": b.backend_name, "version": b.version} for b in backends
        ],
    }


async def _runtime_or_404(
    repos: Repositories, workspace_id: UUID, runtime_id: UUID
) -> orm.Runtime:
    runtime = await repos.runtimes.get(runtime_id)
    if runtime is None or runtime.workspace_id != workspace_id:
        raise HTTPException(status_code=404, detail="runtime not found")
    return runtime


class _RuntimeCreate(BaseModel):
    hostname: str
    os: str


class _BackendsReport(BaseModel):
    backends: list[dict]


@router.get("/api/workspaces/{workspace_id}/runtimes")
async def list_runtimes(
    workspace_id: UUID, repos: Repositories = Depends(get_repositories)
) -> list[dict]:
    runtimes = await repos.runtimes.list_for_workspace(workspace_id)
    return [await _runtime_payload(repos, r) for r in runtimes]


@router.post("/api/workspaces/{workspace_id}/runtimes", status_code=201)
async def register_runtime(
    workspace_id: UUID,
    body: _RuntimeCreate,
    repos: Repositories = Depends(get_repositories),
) -> dict:
    plaintext, token_hash = issue_runtime_token()
    runtime = orm.Runtime(
        id=uuid4(),
        workspace_id=workspace_id,
        hostname=body.hostname.strip(),
        os=body.os,
        token_hash=token_hash,
        status=RuntimeStatus.ONLINE.value,
        last_seen_at=None,
        created_at=datetime.now(UTC),
    )
    await repos.runtimes.add(runtime)
    payload = await _runtime_payload(repos, runtime)
    payload["token"] = plaintext
    return payload


@router.get("/api/workspaces/{workspace_id}/runtimes/{runtime_id}")
async def get_runtime(
    workspace_id: UUID,
    runtime_id: UUID,
    repos: Repositories = Depends(get_repositories),
) -> dict:
    runtime = await _runtime_or_404(repos, workspace_id, runtime_id)
    return await _runtime_payload(repos, runtime)


@router.post("/api/workspaces/{workspace_id}/runtimes/{runtime_id}/heartbeat")
async def heartbeat(
    workspace_id: UUID,
    runtime_id: UUID,
    repos: Repositories = Depends(get_repositories),
) -> dict:
    runtime = await _runtime_or_404(repos, workspace_id, runtime_id)
    runtime.last_seen_at = datetime.now(UTC)
    return await _runtime_payload(repos, runtime)


@router.post("/api/workspaces/{workspace_id}/runtimes/{runtime_id}/backends")
async def report_backends(
    workspace_id: UUID,
    runtime_id: UUID,
    body: _BackendsReport,
    repos: Repositories = Depends(get_repositories),
) -> dict:
    runtime = await _runtime_or_404(repos, workspace_id, runtime_id)
    for existing in await repos.runtime_backends.list_for_runtime(runtime_id):
        await repos.runtime_backends.delete(existing)
    for backend in body.backends:
        await repos.runtime_backends.add(
            orm.RuntimeBackend(
                id=uuid4(),
                runtime_id=runtime_id,
                backend_name=backend["name"],
                version=backend.get("version"),
                probed_at=datetime.now(UTC),
            )
        )
    return await _runtime_payload(repos, runtime)


@router.post("/api/workspaces/{workspace_id}/runtimes/{runtime_id}/revoke")
async def revoke_runtime(
    workspace_id: UUID,
    runtime_id: UUID,
    repos: Repositories = Depends(get_repositories),
) -> dict:
    runtime = await _runtime_or_404(repos, workspace_id, runtime_id)
    runtime.status = RuntimeStatus.DISABLED.value
    return await _runtime_payload(repos, runtime)
