"""Inbox REST API contract (§5.2.6).

Pins the human-intervention surface: the worker posts an inbox item when an
APPROVAL_REQUEST / clarification / failed event needs a person; the Web client
lists workspace items and resolves / assigns / dismisses them. State
transitions are enforced by the domain entity. Tests run against the live
``orchestratord_test`` database and skip when Postgres is unreachable.

Reference: docs/FEATURE_GAP_VS_MULTICA.md §5.2.6.
"""
from __future__ import annotations

from uuid import uuid4

import pytest

pytestmark = pytest.mark.database


async def _create(client, ws, **overrides) -> dict:
    payload = {"kind": "approval_request", "title": "approve the thing"}
    payload.update(overrides)
    resp = await client.post(f"/api/workspaces/{ws}/inbox", json=payload)
    assert resp.status_code == 201, resp.text
    return resp.json()


class TestCreate:
    async def test_create_returns_item(self, client) -> None:
        ws = uuid4()
        body = await _create(client, ws)
        assert body["kind"] == "approval_request"
        assert body["status"] == "open"
        assert body["workspace_id"] == str(ws)

    async def test_create_invalid_kind_422(self, client) -> None:
        ws = uuid4()
        resp = await client.post(
            f"/api/workspaces/{ws}/inbox", json={"kind": "mention", "title": "x"}
        )
        assert resp.status_code == 422


class TestList:
    async def test_scoped_to_workspace(self, client) -> None:
        ws_a, ws_b = uuid4(), uuid4()
        await _create(client, ws_a)
        assert (await client.get(f"/api/workspaces/{ws_b}/inbox")).json() == []

    async def test_filter_by_status(self, client) -> None:
        ws = uuid4()
        await _create(client, ws, kind="failed")
        resolved = await _create(client, ws)
        await client.post(f"/api/workspaces/{ws}/inbox/{resolved['id']}/resolve")
        resolved_items = (
            await client.get(
                f"/api/workspaces/{ws}/inbox", params={"status": "resolved"}
            )
        ).json()
        open_items = (
            await client.get(
                f"/api/workspaces/{ws}/inbox", params={"status": "open"}
            )
        ).json()
        assert len(resolved_items) == 1
        assert len(open_items) == 1


class TestDetail:
    async def test_cross_workspace_404(self, client) -> None:
        ws_a, ws_b = uuid4(), uuid4()
        item = await _create(client, ws_a)
        resp = await client.get(f"/api/workspaces/{ws_b}/inbox/{item['id']}")
        assert resp.status_code == 404


class TestAssign:
    async def test_assign(self, client) -> None:
        ws = uuid4()
        item = await _create(client, ws)
        resp = await client.post(
            f"/api/workspaces/{ws}/inbox/{item['id']}/assign",
            json={"assignee_type": "member", "assignee_id": str(uuid4())},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "assigned"
        assert resp.json()["assignee_type"] == "member"

    async def test_assign_invalid_type_422(self, client) -> None:
        ws = uuid4()
        item = await _create(client, ws)
        resp = await client.post(
            f"/api/workspaces/{ws}/inbox/{item['id']}/assign",
            json={"assignee_type": "bot", "assignee_id": str(uuid4())},
        )
        assert resp.status_code == 422

    async def test_assign_non_open_409(self, client) -> None:
        ws = uuid4()
        item = await _create(client, ws)
        await client.post(f"/api/workspaces/{ws}/inbox/{item['id']}/resolve")
        resp = await client.post(
            f"/api/workspaces/{ws}/inbox/{item['id']}/assign",
            json={"assignee_type": "member", "assignee_id": str(uuid4())},
        )
        assert resp.status_code == 409


class TestResolve:
    async def test_resolve(self, client) -> None:
        ws = uuid4()
        item = await _create(client, ws)
        resp = await client.post(f"/api/workspaces/{ws}/inbox/{item['id']}/resolve")
        assert resp.status_code == 200
        assert resp.json()["status"] == "resolved"

    async def test_resolve_twice_409(self, client) -> None:
        ws = uuid4()
        item = await _create(client, ws)
        await client.post(f"/api/workspaces/{ws}/inbox/{item['id']}/resolve")
        resp = await client.post(f"/api/workspaces/{ws}/inbox/{item['id']}/resolve")
        assert resp.status_code == 409


class TestDismiss:
    async def test_dismiss(self, client) -> None:
        ws = uuid4()
        item = await _create(client, ws)
        resp = await client.post(f"/api/workspaces/{ws}/inbox/{item['id']}/dismiss")
        assert resp.status_code == 200
        assert resp.json()["status"] == "dismissed"

    async def test_dismiss_twice_409(self, client) -> None:
        ws = uuid4()
        item = await _create(client, ws)
        await client.post(f"/api/workspaces/{ws}/inbox/{item['id']}/dismiss")
        resp = await client.post(f"/api/workspaces/{ws}/inbox/{item['id']}/dismiss")
        assert resp.status_code == 409
