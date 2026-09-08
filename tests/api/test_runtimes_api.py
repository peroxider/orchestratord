"""Runtimes REST API contract (§5.2.7, §6.3).

Pins the runtime wire contract: registration returns a one-time plaintext
token (never stored), while list/detail render a runtime card that never
exposes the token or its hash. Heartbeat, backend-probe reporting, and
revoke are the daemon uplink verbs. Tests run against the live
``orchestratord_test`` database and skip when Postgres is unreachable.

Reference: docs/FEATURE_GAP_VS_MULTICA.md §5.2.7, §6.3.
"""
from __future__ import annotations

from uuid import uuid4

import pytest

pytestmark = pytest.mark.database


async def _register(client, ws, **overrides) -> dict:
    payload = {"hostname": "build-01", "os": "linux"}
    payload.update(overrides)
    resp = await client.post(f"/api/workspaces/{ws}/runtimes", json=payload)
    assert resp.status_code == 201, resp.text
    return resp.json()


class TestRegister:
    async def test_register_returns_card_and_one_time_token(self, client) -> None:
        ws = uuid4()
        body = await _register(client, ws)
        assert body["hostname"] == "build-01"
        assert body["status"] == "online"
        assert body["token"]
        assert "token_hash" not in body

    async def test_token_not_in_list_or_detail(self, client) -> None:
        ws = uuid4()
        runtime = await _register(client, ws)
        listed = (await client.get(f"/api/workspaces/{ws}/runtimes")).json()
        assert "token" not in listed[0]
        detail = (
            await client.get(f"/api/workspaces/{ws}/runtimes/{runtime['id']}")
        ).json()
        assert "token" not in detail
        assert "token_hash" not in detail


class TestList:
    async def test_list_scoped_to_workspace(self, client) -> None:
        ws_a, ws_b = uuid4(), uuid4()
        await _register(client, ws_a)
        assert (await client.get(f"/api/workspaces/{ws_b}/runtimes")).json() == []


class TestHeartbeat:
    async def test_heartbeat_updates_last_seen(self, client) -> None:
        ws = uuid4()
        runtime = await _register(client, ws)
        assert runtime["last_seen_at"] is None
        resp = await client.post(
            f"/api/workspaces/{ws}/runtimes/{runtime['id']}/heartbeat"
        )
        assert resp.status_code == 200
        assert resp.json()["last_seen_at"] is not None


class TestBackends:
    async def test_report_backends(self, client) -> None:
        ws = uuid4()
        runtime = await _register(client, ws)
        resp = await client.post(
            f"/api/workspaces/{ws}/runtimes/{runtime['id']}/backends",
            json={"backends": [{"name": "codex", "version": "1.0"}]},
        )
        assert resp.status_code == 200
        assert resp.json()["probed_backends"] == [
            {"name": "codex", "version": "1.0"}
        ]


class TestRevoke:
    async def test_revoke_disables(self, client) -> None:
        ws = uuid4()
        runtime = await _register(client, ws)
        resp = await client.post(f"/api/workspaces/{ws}/runtimes/{runtime['id']}/revoke")
        assert resp.status_code == 200
        assert resp.json()["status"] == "disabled"


class TestNotFound:
    async def test_get_unknown_404(self, client) -> None:
        ws = uuid4()
        resp = await client.get(f"/api/workspaces/{ws}/runtimes/{uuid4()}")
        assert resp.status_code == 404

    async def test_cross_workspace_404(self, client) -> None:
        ws_a, ws_b = uuid4(), uuid4()
        runtime = await _register(client, ws_a)
        resp = await client.get(f"/api/workspaces/{ws_b}/runtimes/{runtime['id']}")
        assert resp.status_code == 404
