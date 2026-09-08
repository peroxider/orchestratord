from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from orchestratord.db.models.tenancy import Workspace

pytestmark = pytest.mark.database


async def test_instance_resolves_hidden_default_workspace(client, db) -> None:
    workspace = Workspace(
        id=uuid4(),
        slug="default",
        name="Local control plane",
        created_at=datetime.now(UTC),
    )
    db.add(workspace)
    await db.commit()

    response = await client.get("/api/instance")

    assert response.status_code == 200
    assert response.json() == {
        "instance_name": "orchestratord",
        "workspace_id": str(workspace.id),
        "workspace_name": "Local control plane",
        "server_version": "0.1.0",
        "realtime_url": "ws://127.0.0.1:9000/ws",
        "features": {},
    }


async def test_instance_reports_uninitialized_local_state(client) -> None:
    response = await client.get("/api/instance")

    assert response.status_code == 503
    assert "not initialized" in response.json()["detail"]
