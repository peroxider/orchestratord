"""Autopilots REST API contract (§7.3).

Pins the autopilot wire contract: workspace-scoped create/list/detail, an
``enabled`` toggle, and a run-history sub-resource. Tests run against the live
``orchestratord_test`` database and skip when Postgres is unreachable.

Reference: docs/FEATURE_GAP_VS_MULTICA.md §7.3.
"""
from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

pytestmark = pytest.mark.database


async def _create(client, ws, **overrides) -> dict:
    payload = {
        "name": "nightly",
        "cron": "0 2 * * *",
        "prompt": "run regression",
        "target_kind": "issue",
        "target_id": str(uuid4()),
    }
    payload.update(overrides)
    resp = await client.post(f"/api/workspaces/{ws}/autopilots", json=payload)
    assert resp.status_code == 201, resp.text
    return resp.json()


class TestCreate:
    async def test_create_returns_autopilot(self, client) -> None:
        ws = uuid4()
        body = await _create(client, ws, name="nightly")
        assert body["name"] == "nightly"
        assert body["enabled"] is True
        assert body["workspace_id"] == str(ws)


class TestList:
    async def test_list_scoped_to_workspace(self, client) -> None:
        ws_a, ws_b = uuid4(), uuid4()
        await _create(client, ws_a)
        assert (await client.get(f"/api/workspaces/{ws_b}/autopilots")).json() == []


class TestToggle:
    async def test_patch_enabled(self, client) -> None:
        ws = uuid4()
        autopilot = await _create(client, ws)
        resp = await client.patch(
            f"/api/workspaces/{ws}/autopilots/{autopilot['id']}",
            json={"enabled": False},
        )
        assert resp.status_code == 200
        assert resp.json()["enabled"] is False

    async def test_patch_unknown_404(self, client) -> None:
        ws = uuid4()
        resp = await client.patch(
            f"/api/workspaces/{ws}/autopilots/{uuid4()}", json={"enabled": False}
        )
        assert resp.status_code == 404


class TestRuns:
    async def test_record_run(self, client) -> None:
        ws = uuid4()
        autopilot = await _create(client, ws)
        run_id = uuid4()
        resp = await client.post(
            f"/api/workspaces/{ws}/autopilots/{autopilot['id']}/runs",
            json={
                "scheduled_at": datetime.now(UTC).isoformat(),
                "run_id": str(run_id),
                "status": "completed",
            },
        )
        assert resp.status_code == 201
        assert resp.json()["run_id"] == str(run_id)
        detail = (
            await client.get(f"/api/workspaces/{ws}/autopilots/{autopilot['id']}")
        ).json()
        assert [r["run_id"] for r in detail["runs"]] == [str(run_id)]

    async def test_record_run_unknown_404(self, client) -> None:
        ws = uuid4()
        resp = await client.post(
            f"/api/workspaces/{ws}/autopilots/{uuid4()}/runs",
            json={
                "scheduled_at": datetime.now(UTC).isoformat(),
                "run_id": str(uuid4()),
                "status": "running",
            },
        )
        assert resp.status_code == 404
