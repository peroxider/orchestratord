"""Single-operator Web bootstrap contract.

The database keeps a workspace boundary, while the browser sees one local
orchestratord instance.  This endpoint is intentionally the only place the
Web shell resolves that hidden workspace.
"""

from __future__ import annotations

import os

from fastapi import APIRouter, Depends, HTTPException

from orchestratord.api.db import get_repositories
from orchestratord.db.repository import Repositories
from orchestratord.seed import DEFAULT_WORKSPACE_SLUG

router = APIRouter(tags=["instance"])

_WEB_APPLICATIONS = [
    {
        "id": "issue_pr",
        "enabled": True,
        "capabilities": [
            "issues.read",
            "issues.write",
            "pull_requests.read",
            "clarification.respond",
        ],
    }
]


@router.get("/api/instance")
async def get_instance(
    repos: Repositories = Depends(get_repositories),  # noqa: B008 - FastAPI DI
) -> dict:
    workspace = await repos.workspaces.by_slug(DEFAULT_WORKSPACE_SLUG)
    if workspace is None:
        raise HTTPException(
            status_code=503,
            detail="local instance is not initialized; run the default seed",
        )
    return {
        "instance_name": os.environ.get("ORCHESTRATORD_INSTANCE_NAME", "orchestratord"),
        "workspace_id": str(workspace.id),
        "workspace_name": workspace.name,
        "server_version": "0.1.0",
        "realtime_url": os.environ.get("ORCHESTRATORD_REALTIME_URL", "ws://127.0.0.1:9000/ws"),
        "applications": _WEB_APPLICATIONS,
        "features": {"applications": _WEB_APPLICATIONS},
    }
