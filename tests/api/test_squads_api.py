"""Squads REST API contract (§7.1).

Pins the squad wire contract: create/list/detail/soft-delete over the
repository layer, plus ``assign`` (leader hands an issue to a member) and
``route`` (auto-route to the first non-leader candidate). Leader/member
polymorphism is enforced by the domain ``Squad``; the agent-leader goal-mode
gate is enforced at the API layer, which resolves the leader through the
agent repository.

Reference: docs/FEATURE_GAP_VS_MULTICA.md §7.1.
"""
from __future__ import annotations

from uuid import uuid4

import pytest

from orchestratord.domain.agent import CORE_CAPABILITY_BITS

pytestmark = pytest.mark.database


def _member_payload(member_type: str = "member", member_id=None) -> dict:
    return {"member_type": member_type, "member_id": str(member_id or uuid4())}


async def _create(client, ws, **overrides) -> dict:
    payload = {
        "name": "core-team",
        "leader_type": "member",
        "leader_id": str(uuid4()),
        "members": [_member_payload()],
    }
    payload.update(overrides)
    resp = await client.post(f"/api/workspaces/{ws}/squads", json=payload)
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _create_agent(client, ws, **overrides) -> dict:
    payload = {
        "name": "agent",
        "provider": "codex",
        "runtime_id": str(uuid4()),
        "capabilities_cache_jsonb": {bit: True for bit in CORE_CAPABILITY_BITS},
    }
    payload.update(overrides)
    resp = await client.post(f"/api/workspaces/{ws}/agents", json=payload)
    assert resp.status_code == 201, resp.text
    return resp.json()


class TestCreate:
    async def test_create_returns_squad(self, client) -> None:
        ws = uuid4()
        body = await _create(client, ws, name="triage")
        assert body["name"] == "triage"
        assert body["workspace_id"] == str(ws)
        assert body["leader_type"] == "member"
        assert body["id"]

    async def test_create_invalid_leader_type_rejected(self, client) -> None:
        ws = uuid4()
        resp = await client.post(
            f"/api/workspaces/{ws}/squads",
            json={
                "name": "x",
                "leader_type": "bot",
                "leader_id": str(uuid4()),
                "members": [],
            },
        )
        assert resp.status_code == 422

    async def test_create_invalid_member_type_rejected(self, client) -> None:
        ws = uuid4()
        resp = await client.post(
            f"/api/workspaces/{ws}/squads",
            json={
                "name": "x",
                "leader_type": "member",
                "leader_id": str(uuid4()),
                "members": [_member_payload(member_type="service_account")],
            },
        )
        assert resp.status_code == 422

    async def test_create_agent_leader_without_goal_mode_rejected(self, client) -> None:
        ws = uuid4()
        agent = await _create_agent(client, ws, name="no-goal")
        resp = await client.post(
            f"/api/workspaces/{ws}/squads",
            json={
                "name": "x",
                "leader_type": "agent",
                "leader_id": agent["id"],
                "members": [],
            },
        )
        assert resp.status_code == 422

    async def test_create_agent_leader_with_goal_mode_accepted(self, client) -> None:
        ws = uuid4()
        caps = {**{bit: True for bit in CORE_CAPABILITY_BITS}, "goal_mode": True}
        agent = await _create_agent(
            client, ws, name="goal-ok", capabilities_cache_jsonb=caps
        )
        resp = await client.post(
            f"/api/workspaces/{ws}/squads",
            json={
                "name": "x",
                "leader_type": "agent",
                "leader_id": agent["id"],
                "members": [],
            },
        )
        assert resp.status_code == 201

    async def test_create_agent_leader_unknown_agent_rejected(self, client) -> None:
        ws = uuid4()
        resp = await client.post(
            f"/api/workspaces/{ws}/squads",
            json={
                "name": "x",
                "leader_type": "agent",
                "leader_id": str(uuid4()),
                "members": [],
            },
        )
        assert resp.status_code == 404

    async def test_create_agent_leader_cross_workspace_rejected(self, client) -> None:
        ws_a, ws_b = uuid4(), uuid4()
        caps = {**{bit: True for bit in CORE_CAPABILITY_BITS}, "goal_mode": True}
        agent = await _create_agent(
            client, ws_a, name="goal-ok", capabilities_cache_jsonb=caps
        )
        resp = await client.post(
            f"/api/workspaces/{ws_b}/squads",
            json={
                "name": "x",
                "leader_type": "agent",
                "leader_id": agent["id"],
                "members": [],
            },
        )
        assert resp.status_code == 404


class TestList:
    async def test_list_scoped_to_workspace(self, client) -> None:
        ws_a, ws_b = uuid4(), uuid4()
        await _create(client, ws_a)
        resp = await client.get(f"/api/workspaces/{ws_b}/squads")
        assert resp.status_code == 200
        assert resp.json() == []

    async def test_list_returns_created_squad(self, client) -> None:
        ws = uuid4()
        await _create(client, ws, name="triage")
        resp = await client.get(f"/api/workspaces/{ws}/squads")
        assert [s["name"] for s in resp.json()] == ["triage"]


class TestDetail:
    async def test_get_squad(self, client) -> None:
        ws = uuid4()
        squad = await _create(client, ws)
        resp = await client.get(f"/api/workspaces/{ws}/squads/{squad['id']}")
        assert resp.status_code == 200
        assert resp.json()["id"] == squad["id"]

    async def test_get_unknown_squad_404(self, client) -> None:
        ws = uuid4()
        resp = await client.get(f"/api/workspaces/{ws}/squads/{uuid4()}")
        assert resp.status_code == 404

    async def test_get_cross_workspace_404(self, client) -> None:
        ws_a, ws_b = uuid4(), uuid4()
        squad = await _create(client, ws_a)
        resp = await client.get(f"/api/workspaces/{ws_b}/squads/{squad['id']}")
        assert resp.status_code == 404


class TestDelete:
    async def test_soft_delete(self, client) -> None:
        ws = uuid4()
        squad = await _create(client, ws)
        resp = await client.delete(f"/api/workspaces/{ws}/squads/{squad['id']}")
        assert resp.status_code == 204
        assert (await client.get(f"/api/workspaces/{ws}/squads")).json() == []
        assert (
            await client.get(f"/api/workspaces/{ws}/squads/{squad['id']}")
        ).status_code == 404


class TestAssign:
    async def test_assign_to_member(self, client) -> None:
        ws = uuid4()
        member_id = uuid4()
        squad = await _create(client, ws, members=[_member_payload(member_id=member_id)])
        resp = await client.post(
            f"/api/squads/{squad['id']}/assign",
            json={
                "issue_id": str(uuid4()),
                "member_type": "member",
                "member_id": str(member_id),
            },
        )
        assert resp.status_code == 202
        assert resp.json()["assigned"] is True

    async def test_assign_unknown_member_404(self, client) -> None:
        ws = uuid4()
        squad = await _create(client, ws)
        resp = await client.post(
            f"/api/squads/{squad['id']}/assign",
            json={
                "issue_id": str(uuid4()),
                "member_type": "member",
                "member_id": str(uuid4()),
            },
        )
        assert resp.status_code == 404


class TestRoute:
    async def test_route_picks_candidate(self, client) -> None:
        ws = uuid4()
        member_id = uuid4()
        squad = await _create(client, ws, members=[_member_payload(member_id=member_id)])
        resp = await client.post(
            f"/api/squads/{squad['id']}/route", json={"issue_id": str(uuid4())}
        )
        assert resp.status_code == 202
        assert resp.json()["member_id"] == str(member_id)

    async def test_route_no_candidates_422(self, client) -> None:
        ws = uuid4()
        squad = await _create(client, ws, members=[])
        resp = await client.post(
            f"/api/squads/{squad['id']}/route", json={"issue_id": str(uuid4())}
        )
        assert resp.status_code == 422
