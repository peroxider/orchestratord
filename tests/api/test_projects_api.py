"""Projects REST API contract (§7.2).

Pins the project wire contract: workspace-scoped create/list/detail plus the
repo and doc sub-resources. ``doc_type`` validation (md/html/pdf) is enforced
by the domain model; every read re-checks ``workspace_id`` for tenant
isolation. Tests run against the live ``orchestratord_test`` database (each
test is truncated up front) and skip when Postgres is unreachable.

Reference: docs/FEATURE_GAP_VS_MULTICA.md §7.2.
"""
from __future__ import annotations

from uuid import uuid4

import pytest

pytestmark = pytest.mark.database


async def _create(client, ws, **overrides) -> dict:
    payload = {"name": "webapp", "description": "the web app"}
    payload.update(overrides)
    resp = await client.post(f"/api/workspaces/{ws}/projects", json=payload)
    assert resp.status_code == 201, resp.text
    return resp.json()


class TestCreate:
    async def test_create_returns_project(self, client) -> None:
        ws = uuid4()
        body = await _create(client, ws, name="webapp")
        assert body["name"] == "webapp"
        assert body["workspace_id"] == str(ws)
        assert body["repos"] == []
        assert body["docs"] == []


class TestList:
    async def test_list_scoped_to_workspace(self, client) -> None:
        ws_a, ws_b = uuid4(), uuid4()
        await _create(client, ws_a)
        assert (await client.get(f"/api/workspaces/{ws_b}/projects")).json() == []

    async def test_list_returns_project(self, client) -> None:
        ws = uuid4()
        await _create(client, ws, name="webapp")
        resp = await client.get(f"/api/workspaces/{ws}/projects")
        assert [p["name"] for p in resp.json()] == ["webapp"]


class TestDetail:
    async def test_get_unknown_404(self, client) -> None:
        ws = uuid4()
        resp = await client.get(f"/api/workspaces/{ws}/projects/{uuid4()}")
        assert resp.status_code == 404

    async def test_get_cross_workspace_404(self, client) -> None:
        ws_a, ws_b = uuid4(), uuid4()
        project = await _create(client, ws_a)
        resp = await client.get(f"/api/workspaces/{ws_b}/projects/{project['id']}")
        assert resp.status_code == 404


class TestRepos:
    async def test_add_repo(self, client) -> None:
        ws = uuid4()
        project = await _create(client, ws)
        resp = await client.post(
            f"/api/workspaces/{ws}/projects/{project['id']}/repos",
            json={"repo_url": "https://git/x.git", "default_branch": "develop"},
        )
        assert resp.status_code == 201
        assert resp.json()["default_branch"] == "develop"
        detail = (
            await client.get(f"/api/workspaces/{ws}/projects/{project['id']}")
        ).json()
        assert detail["repos"] == [
            {"repo_url": "https://git/x.git", "default_branch": "develop"}
        ]


class TestDocs:
    async def test_add_doc(self, client) -> None:
        ws = uuid4()
        project = await _create(client, ws)
        resp = await client.post(
            f"/api/workspaces/{ws}/projects/{project['id']}/docs",
            json={"doc_url": "https://docs", "doc_type": "md"},
        )
        assert resp.status_code == 201

    async def test_add_doc_invalid_type_422(self, client) -> None:
        ws = uuid4()
        project = await _create(client, ws)
        resp = await client.post(
            f"/api/workspaces/{ws}/projects/{project['id']}/docs",
            json={"doc_url": "https://docs", "doc_type": "pdf-invalid"},
        )
        assert resp.status_code == 422
