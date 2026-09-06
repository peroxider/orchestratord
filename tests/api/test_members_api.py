"""Members REST API contract (§5.7.1).

Pins the multi-tenancy membership surface: a workspace owns members carrying
one of three roles (owner / admin / member); ``member_agent_scopes`` grants a
member access to specific agents. Role is validated at the API boundary.
Tests run against the live ``orchestratord_test`` database and skip when
Postgres is unreachable.

Reference: docs/FEATURE_GAP_VS_MULTICA.md §5.7.1.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

pytestmark = pytest.mark.database


async def _create(client, ws, **overrides) -> dict:
    payload = {"role": "member"}
    payload.update(overrides)
    resp = await client.post(f"/api/workspaces/{ws}/members", json=payload)
    assert resp.status_code == 201, resp.text
    return resp.json()


class TestCreate:
    async def test_create_returns_member(self, client) -> None:
        ws = uuid4()
        body = await _create(client, ws)
        assert body["role"] == "member"
        assert body["workspace_id"] == str(ws)

    async def test_create_all_roles(self, client) -> None:
        ws = uuid4()
        for role in ("owner", "admin", "member"):
            assert (await _create(client, ws, role=role))["role"] == role

    async def test_create_invalid_role_422(self, client) -> None:
        ws = uuid4()
        resp = await client.post(
            f"/api/workspaces/{ws}/members", json={"role": "viewer"}
        )
        assert resp.status_code == 422


class TestList:
    async def test_scoped_to_workspace(self, client) -> None:
        ws_a, ws_b = uuid4(), uuid4()
        await _create(client, ws_a)
        assert (await client.get(f"/api/workspaces/{ws_b}/members")).json() == []


class TestDetail:
    async def test_cross_workspace_404(self, client) -> None:
        ws_a, ws_b = uuid4(), uuid4()
        member = await _create(client, ws_a)
        resp = await client.get(f"/api/workspaces/{ws_b}/members/{member['id']}")
        assert resp.status_code == 404


class TestUpdate:
    async def test_update_role(self, client) -> None:
        ws = uuid4()
        member = await _create(client, ws)
        resp = await client.patch(
            f"/api/workspaces/{ws}/members/{member['id']}", json={"role": "admin"}
        )
        assert resp.status_code == 200
        assert resp.json()["role"] == "admin"

    async def test_update_invalid_role_422(self, client) -> None:
        ws = uuid4()
        member = await _create(client, ws)
        resp = await client.patch(
            f"/api/workspaces/{ws}/members/{member['id']}", json={"role": "viewer"}
        )
        assert resp.status_code == 422


class TestDelete:
    async def test_delete_then_404(self, client) -> None:
        ws = uuid4()
        member = await _create(client, ws)
        resp = await client.delete(f"/api/workspaces/{ws}/members/{member['id']}")
        assert resp.status_code == 204
        assert (
            await client.get(f"/api/workspaces/{ws}/members/{member['id']}")
        ).status_code == 404


class TestScopes:
    async def test_grant_and_list(self, client) -> None:
        ws = uuid4()
        member = await _create(client, ws)
        agent_id = uuid4()
        resp = await client.post(
            f"/api/workspaces/{ws}/members/{member['id']}/scopes",
            json={"agent_id": str(agent_id)},
        )
        assert resp.status_code == 200
        listed = (
            await client.get(f"/api/workspaces/{ws}/members/{member['id']}/scopes")
        ).json()
        assert listed["agent_ids"] == [str(agent_id)]

    async def test_revoke_scope(self, client) -> None:
        ws = uuid4()
        member = await _create(client, ws)
        agent_id = uuid4()
        await client.post(
            f"/api/workspaces/{ws}/members/{member['id']}/scopes",
            json={"agent_id": str(agent_id)},
        )
        resp = await client.delete(
            f"/api/workspaces/{ws}/members/{member['id']}/scopes/{agent_id}"
        )
        assert resp.status_code == 204
        listed = (
            await client.get(f"/api/workspaces/{ws}/members/{member['id']}/scopes")
        ).json()
        assert listed["agent_ids"] == []

    async def test_scope_cross_workspace_404(self, client) -> None:
        ws_a, ws_b = uuid4(), uuid4()
        member = await _create(client, ws_a)
        resp = await client.post(
            f"/api/workspaces/{ws_b}/members/{member['id']}/scopes",
            json={"agent_id": str(uuid4())},
        )
        assert resp.status_code == 404
