"""``GET /api/workspaces/by-slug/{slug}`` contract.

Phase-1 console URLs carry the workspace slug while every
workspace-scoped route validates a UUID path param; the dashboard
resolves the slug through this endpoint once per workspace
(``apps/web/components/dashboard-guard.tsx``).
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from orchestratord.db import models as orm


async def test_by_slug_returns_workspace_id(client, db) -> None:
    ws_id = uuid4()
    db.add(
        orm.Workspace(
            id=ws_id,
            slug="resolve-me",
            name="Resolver",
            created_at=datetime.now(UTC),
        )
    )
    await db.commit()

    resp = await client.get("/api/workspaces/by-slug/resolve-me")
    assert resp.status_code == 200
    assert resp.json() == {
        "workspace_id": str(ws_id),
        "slug": "resolve-me",
        "name": "Resolver",
    }


async def test_by_slug_404_for_unknown(client) -> None:
    resp = await client.get("/api/workspaces/by-slug/__no_such__")
    assert resp.status_code == 404
