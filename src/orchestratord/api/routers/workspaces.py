"""Workspace lookup routes.

Phase-1 console entry: the URL carries the workspace *slug*
(``/default/issues``), but every workspace-scoped API route validates a
UUID path parameter.  The dashboard resolves the slug to the real
workspace id once via ``GET /api/workspaces/by-slug/{slug}`` and stores
the UUID (``apps/web/components/dashboard-guard.tsx``).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from orchestratord.api.db import get_repositories
from orchestratord.db.repository import Repositories

router = APIRouter(prefix="/api/workspaces", tags=["workspaces"])


@router.get("/by-slug/{slug}")
async def get_workspace_by_slug(
    slug: str,
    repos: Repositories = Depends(get_repositories),
) -> dict:
    workspace = await repos.workspaces.by_slug(slug)
    if workspace is None:
        raise HTTPException(status_code=404, detail=f"unknown workspace {slug!r}")
    return {
        "workspace_id": str(workspace.id),
        "slug": workspace.slug,
        "name": workspace.name,
    }
