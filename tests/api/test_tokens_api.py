"""API tokens REST API contract (§5.7.4).

Pins the token wire contract: creation returns a one-time plaintext token
(never stored), while list/detail render a token card that never exposes the
token or its hash. Revoke deletes the token. Tests run against the live
``orchestratord_test`` database and skip when Postgres is unreachable.

Reference: docs/FEATURE_GAP_VS_MULTICA.md §5.7.4.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

pytestmark = pytest.mark.database


async def _create(client, ws, **overrides) -> dict:
    payload = {"name": "ci", "scopes": ["issues:read"]}
    payload.update(overrides)
    resp = await client.post(f"/api/workspaces/{ws}/tokens", json=payload)
    assert resp.status_code == 201, resp.text
    return resp.json()


class TestCreate:
    async def test_create_returns_token_once(self, client) -> None:
        ws = uuid4()
        body = await _create(client, ws)
        assert body["name"] == "ci"
        assert body["scopes"] == ["issues:read"]
        assert body["token"]
        assert "token_hash" not in body

    async def test_token_not_in_list_or_detail(self, client) -> None:
        ws = uuid4()
        token = await _create(client, ws)
        listed = (await client.get(f"/api/workspaces/{ws}/tokens")).json()
        assert "token" not in listed[0]
        assert "token_hash" not in listed[0]
        detail = (
            await client.get(f"/api/workspaces/{ws}/tokens/{token['id']}")
        ).json()
        assert "token" not in detail
        assert "token_hash" not in detail


class TestList:
    async def test_scoped_to_workspace(self, client) -> None:
        ws_a, ws_b = uuid4(), uuid4()
        await _create(client, ws_a)
        assert (await client.get(f"/api/workspaces/{ws_b}/tokens")).json() == []


class TestDetail:
    async def test_cross_workspace_404(self, client) -> None:
        ws_a, ws_b = uuid4(), uuid4()
        token = await _create(client, ws_a)
        resp = await client.get(f"/api/workspaces/{ws_b}/tokens/{token['id']}")
        assert resp.status_code == 404


class TestRevoke:
    async def test_delete_then_404(self, client) -> None:
        ws = uuid4()
        token = await _create(client, ws)
        resp = await client.delete(f"/api/workspaces/{ws}/tokens/{token['id']}")
        assert resp.status_code == 204
        assert (
            await client.get(f"/api/workspaces/{ws}/tokens/{token['id']}")
        ).status_code == 404
