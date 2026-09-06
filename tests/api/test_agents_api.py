"""Agents REST API contract (§5.2.2).

Pins the workspace-scoped agent registry wire contract: create, list,
capabilities, and doctor. Backed by the ``agents`` table via the repository
layer (§6.2). Tests run against the live ``orchestratord_test`` database and
skip when Postgres is unreachable.

Reference: docs/FEATURE_GAP_VS_MULTICA.md §5.2.2.
"""
from __future__ import annotations

from uuid import uuid4

import pytest

from orchestratord.domain.agent import CORE_CAPABILITY_BITS

pytestmark = pytest.mark.database


def _full_capabilities() -> dict:
    return {bit: True for bit in CORE_CAPABILITY_BITS}


async def _create(client, ws, **overrides) -> dict:
    payload = {
        "name": "alpha",
        "provider": "codex",
        "runtime_id": str(uuid4()),
        "capabilities_cache_jsonb": _full_capabilities(),
    }
    payload.update(overrides)
    resp = await client.post(f"/api/workspaces/{ws}/agents", json=payload)
    assert resp.status_code == 201, resp.text
    return resp.json()


class TestCreate:
    async def test_create_returns_agent(self, client) -> None:
        ws = uuid4()
        body = await _create(client, ws, name="triage-bot")
        assert body["name"] == "triage-bot"
        assert body["workspace_id"] == str(ws)
        assert body["provider"] == "codex"
        assert body["id"]

    async def test_create_unknown_provider_rejected(self, client) -> None:
        ws = uuid4()
        resp = await client.post(
            f"/api/workspaces/{ws}/agents",
            json={
                "name": "x",
                "provider": "__nope__",
                "runtime_id": str(uuid4()),
                "capabilities_cache_jsonb": _full_capabilities(),
            },
        )
        assert resp.status_code == 422

    async def test_create_missing_capability_bits_rejected(self, client) -> None:
        ws = uuid4()
        resp = await client.post(
            f"/api/workspaces/{ws}/agents",
            json={
                "name": "x",
                "provider": "codex",
                "runtime_id": str(uuid4()),
                "capabilities_cache_jsonb": {"streaming_deltas": True},
            },
        )
        assert resp.status_code == 422


class TestList:
    async def test_list_scoped_to_workspace(self, client) -> None:
        ws_a, ws_b = uuid4(), uuid4()
        await _create(client, ws_a)
        resp = await client.get(f"/api/workspaces/{ws_b}/agents")
        assert resp.status_code == 200
        assert resp.json() == []

    async def test_list_returns_created_agent(self, client) -> None:
        ws = uuid4()
        await _create(client, ws, name="triage-bot")
        resp = await client.get(f"/api/workspaces/{ws}/agents")
        assert [a["name"] for a in resp.json()] == ["triage-bot"]


class TestCapabilities:
    async def test_capabilities_round_trip(self, client) -> None:
        ws = uuid4()
        agent = await _create(client, ws)
        resp = await client.get(f"/api/agents/{agent['id']}/capabilities")
        assert resp.status_code == 200
        for bit in CORE_CAPABILITY_BITS:
            assert resp.json()[bit] is True

    async def test_capabilities_unknown_agent_404(self, client) -> None:
        resp = await client.get(f"/api/agents/{uuid4()}/capabilities")
        assert resp.status_code == 404


class TestDoctor:
    async def test_doctor_returns_ready_flag(self, client) -> None:
        ws = uuid4()
        agent = await _create(client, ws)
        resp = await client.post(f"/api/agents/{agent['id']}/doctor")
        assert resp.status_code == 200
        body = resp.json()
        assert body["agent_id"] == agent["id"]
        assert isinstance(body["ready"], bool)
        assert "detail" in body

    async def test_doctor_unknown_agent_404(self, client) -> None:
        resp = await client.post(f"/api/agents/{uuid4()}/doctor")
        assert resp.status_code == 404
