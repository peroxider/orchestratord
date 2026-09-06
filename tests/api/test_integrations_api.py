"""Notification-channel integrations REST API contract (§7.5).

Pins the per-workspace Slack / Lark OAuth-integration registration / listing /
deletion surface. Runs against the live ``orchestratord_test`` database and
skips when Postgres is unreachable.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

pytestmark = pytest.mark.database


async def _register(client, ws, **overrides) -> dict:
    payload = {"provider": "slack", "webhook_url": "https://hooks.slack.com/T/B/X"}
    payload.update(overrides)
    resp = await client.post(f"/api/workspaces/{ws}/integrations", json=payload)
    assert resp.status_code == 201, resp.text
    return resp.json()


class TestRegister:
    async def test_register_returns_integration(self, client) -> None:
        ws = uuid4()
        body = await _register(client, ws)
        assert body["provider"] == "slack"
        assert body["webhook_url"] == "https://hooks.slack.com/T/B/X"
        assert body["workspace_id"] == str(ws)

    async def test_duplicate_provider_409(self, client) -> None:
        ws = uuid4()
        await _register(client, ws)
        resp = await client.post(
            f"/api/workspaces/{ws}/integrations",
            json={"provider": "slack", "webhook_url": "https://other"},
        )
        assert resp.status_code == 409

    async def test_invalid_provider_422(self, client) -> None:
        ws = uuid4()
        resp = await client.post(
            f"/api/workspaces/{ws}/integrations",
            json={"provider": "telegram", "webhook_url": "https://x"},
        )
        assert resp.status_code == 422

    async def test_empty_webhook_url_422(self, client) -> None:
        ws = uuid4()
        resp = await client.post(
            f"/api/workspaces/{ws}/integrations",
            json={"provider": "slack", "webhook_url": "   "},
        )
        assert resp.status_code == 422


class TestList:
    async def test_scoped_to_workspace(self, client) -> None:
        ws_a, ws_b = uuid4(), uuid4()
        await _register(client, ws_a)
        body = (await client.get(f"/api/workspaces/{ws_b}/integrations")).json()
        assert body["integrations"] == []


class TestDelete:
    async def test_delete_then_404(self, client) -> None:
        ws = uuid4()
        inst = await _register(client, ws)
        resp = await client.delete(
            f"/api/workspaces/{ws}/integrations/{inst['id']}"
        )
        assert resp.status_code == 204
        assert (
            await client.get(f"/api/workspaces/{ws}/integrations")
        ).json()["integrations"] == []

    async def test_cross_workspace_delete_404(self, client) -> None:
        ws_a, ws_b = uuid4(), uuid4()
        inst = await _register(client, ws_a)
        resp = await client.delete(
            f"/api/workspaces/{ws_b}/integrations/{inst['id']}"
        )
        assert resp.status_code == 404
