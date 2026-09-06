"""Audit log REST API contract (§5.7.3).

Pins the append-and-read audit surface: POST records an immutable entry whose
``actor_type`` is validated at the API boundary; GET returns workspace-scoped
entries newest-first with actor_type / target_type / action / time-window
filters. Naive / date-only ``from`` / ``to`` must not 500. Tests run against
the live ``orchestratord_test`` database and skip when Postgres is unreachable.

Reference: docs/FEATURE_GAP_VS_MULTICA.md §5.7.3.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

pytestmark = pytest.mark.database


async def _create(client, ws, **overrides) -> dict:
    payload = {
        "actor_type": "member",
        "actor_id": str(uuid4()),
        "action": "issue.create",
        "target_type": "issue",
        "target_id": str(uuid4()),
    }
    payload.update(overrides)
    resp = await client.post(f"/api/workspaces/{ws}/audit", json=payload)
    assert resp.status_code == 201, resp.text
    return resp.json()


class TestAppend:
    async def test_append_returns_entry(self, client) -> None:
        ws = uuid4()
        body = await _create(client, ws)
        assert body["actor_type"] == "member"
        assert body["action"] == "issue.create"
        assert body["workspace_id"] == str(ws)

    async def test_append_all_actor_types(self, client) -> None:
        ws = uuid4()
        for kind in ("member", "agent", "system"):
            body = await _create(client, ws, actor_type=kind, actor_id=str(uuid4()))
            assert body["actor_type"] == kind

    async def test_append_invalid_actor_type_422(self, client) -> None:
        ws = uuid4()
        resp = await client.post(
            f"/api/workspaces/{ws}/audit",
            json={
                "actor_type": "service_account",
                "actor_id": str(uuid4()),
                "action": "issue.create",
                "target_type": "issue",
                "target_id": str(uuid4()),
            },
        )
        assert resp.status_code == 422

    async def test_payload_preserved(self, client) -> None:
        ws = uuid4()
        body = await _create(client, ws, payload_jsonb={"from": "queued", "to": "running"})
        assert body["payload_jsonb"] == {"from": "queued", "to": "running"}


class TestList:
    async def test_scoped_to_workspace(self, client) -> None:
        ws_a, ws_b = uuid4(), uuid4()
        await _create(client, ws_a)
        assert (await client.get(f"/api/workspaces/{ws_b}/audit")).json() == []

    async def test_newest_first(self, client) -> None:
        ws = uuid4()
        await _create(client, ws, action="first")
        await _create(client, ws, action="second")
        listed = (await client.get(f"/api/workspaces/{ws}/audit")).json()
        assert [e["action"] for e in listed] == ["second", "first"]


class TestFilter:
    async def test_filter_by_actor_type(self, client) -> None:
        ws = uuid4()
        await _create(client, ws, actor_type="member", actor_id=str(uuid4()))
        await _create(client, ws, actor_type="agent", actor_id=str(uuid4()))
        listed = (
            await client.get(
                f"/api/workspaces/{ws}/audit", params={"actor_type": "agent"}
            )
        ).json()
        assert [e["actor_type"] for e in listed] == ["agent"]

    async def test_filter_by_target_type(self, client) -> None:
        ws = uuid4()
        await _create(client, ws, target_type="issue", target_id=str(uuid4()))
        await _create(client, ws, target_type="session", target_id=str(uuid4()))
        listed = (
            await client.get(
                f"/api/workspaces/{ws}/audit", params={"target_type": "session"}
            )
        ).json()
        assert [e["target_type"] for e in listed] == ["session"]

    async def test_filter_by_action(self, client) -> None:
        ws = uuid4()
        await _create(client, ws, action="issue.create")
        await _create(client, ws, action="issue.reassign")
        listed = (
            await client.get(
                f"/api/workspaces/{ws}/audit", params={"action": "issue.reassign"}
            )
        ).json()
        assert [e["action"] for e in listed] == ["issue.reassign"]


class TestWindow:
    async def test_naive_from_does_not_500(self, client) -> None:
        ws = uuid4()
        await _create(client, ws, action="keep")
        resp = await client.get(
            f"/api/workspaces/{ws}/audit", params={"from": "2020-01-01T00:00:00"}
        )
        assert resp.status_code == 200
        assert [e["action"] for e in resp.json()] == ["keep"]

    async def test_naive_to_does_not_500(self, client) -> None:
        ws = uuid4()
        await _create(client, ws, action="keep")
        resp = await client.get(
            f"/api/workspaces/{ws}/audit", params={"to": "2030-01-01T00:00:00"}
        )
        assert resp.status_code == 200
        assert [e["action"] for e in resp.json()] == ["keep"]

    async def test_date_only_from_does_not_500(self, client) -> None:
        ws = uuid4()
        await _create(client, ws, action="keep")
        resp = await client.get(
            f"/api/workspaces/{ws}/audit", params={"from": "2020-01-01"}
        )
        assert resp.status_code == 200
        assert [e["action"] for e in resp.json()] == ["keep"]

    async def test_future_from_excludes(self, client) -> None:
        ws = uuid4()
        await _create(client, ws, action="keep")
        resp = await client.get(
            f"/api/workspaces/{ws}/audit", params={"from": "2030-01-01T00:00:00"}
        )
        assert resp.status_code == 200
        assert resp.json() == []
