"""Usage REST API contract (§5.2.4).

Pins the token / cost aggregation surface: a worker posts one ``UsageRecord``
per completed session; the Web client reads workspace-scoped aggregates grouped
by agent / issue / backend / day, or a single agent's usage. ``from`` / ``to``
bound the window; ``group_by`` is validated by the domain aggregator.

``record_usage`` pre-aggregates each record into the ``usage_aggregates`` table
at the ``(workspace_id, agent_id, issue_id, backend, day)`` bucket, so the
read endpoints sum pre-aggregated rows. Per-test isolation comes from the
shared ``TRUNCATE`` in ``tests/api/conftest.py``; agent-scoped reads create
agents via the API.

Reference: docs/FEATURE_GAP_VS_MULTICA.md §5.2.4.
"""
from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from orchestratord.domain.agent import CORE_CAPABILITY_BITS

pytestmark = pytest.mark.database


async def _record(client, ws, **overrides) -> dict:
    payload = {"tokens_in": 10, "tokens_out": 5, "cost_usd": 0.25}
    payload.update(overrides)
    resp = await client.post(f"/api/workspaces/{ws}/usage", json=payload)
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _create_agent(client, ws, **overrides) -> dict:
    payload = {
        "name": "codex",
        "provider": "codex",
        "runtime_id": str(uuid4()),
        "capabilities_cache_jsonb": {bit: True for bit in CORE_CAPABILITY_BITS},
    }
    payload.update(overrides)
    resp = await client.post(f"/api/workspaces/{ws}/agents", json=payload)
    assert resp.status_code == 201, resp.text
    return resp.json()


class TestRecord:
    async def test_record_returns_totals(self, client) -> None:
        body = await _record(client, uuid4())
        assert body["tokens_in"] == 10
        assert body["tokens_out"] == 5
        assert body["tokens_total"] == 15
        assert body["cost_usd"] == 0.25

    async def test_record_negative_tokens_422(self, client) -> None:
        ws = uuid4()
        resp = await client.post(
            f"/api/workspaces/{ws}/usage", json={"tokens_in": -1, "tokens_out": 0}
        )
        assert resp.status_code == 422


class TestListWorkspaceUsage:
    async def test_scoped_to_workspace(self, client) -> None:
        ws_a, ws_b = uuid4(), uuid4()
        await _record(client, ws_a)
        body = (await client.get(f"/api/workspaces/{ws_b}/usage")).json()
        assert body["totals"]["sessions"] == 0

    async def test_totals_aggregate(self, client) -> None:
        ws = uuid4()
        await _record(client, ws, tokens_in=10, tokens_out=5)
        await _record(client, ws, tokens_in=20, tokens_out=5)
        body = (await client.get(f"/api/workspaces/{ws}/usage")).json()
        assert body["totals"]["tokens_in"] == 30
        assert body["totals"]["tokens_out"] == 10
        assert body["totals"]["tokens_total"] == 40
        assert body["totals"]["sessions"] == 2

    async def test_same_bucket_pre_aggregates_sessions(self, client) -> None:
        ws = uuid4()
        agent_id = str(uuid4())
        await _record(
            client, ws, agent_id=agent_id, backend="codex", tokens_in=10, tokens_out=0
        )
        await _record(
            client, ws, agent_id=agent_id, backend="codex", tokens_in=5, tokens_out=0
        )
        body = (
            await client.get(
                f"/api/workspaces/{ws}/usage", params={"group_by": "backend"}
            )
        ).json()
        groups = {g["group"]: g for g in body["groups"]}
        assert groups["codex"]["tokens_in"] == 15
        assert groups["codex"]["sessions"] == 2
        assert body["totals"]["sessions"] == 2

    async def test_group_by_backend(self, client) -> None:
        ws = uuid4()
        await _record(client, ws, backend="codex", tokens_in=10, tokens_out=0)
        await _record(client, ws, backend="claude", tokens_in=5, tokens_out=0)
        body = (
            await client.get(
                f"/api/workspaces/{ws}/usage", params={"group_by": "backend"}
            )
        ).json()
        groups = {g["group"]: g for g in body["groups"]}
        assert groups["codex"]["tokens_in"] == 10
        assert groups["claude"]["tokens_in"] == 5

    async def test_invalid_group_by_422(self, client) -> None:
        ws = uuid4()
        await _record(client, ws)
        resp = await client.get(
            f"/api/workspaces/{ws}/usage", params={"group_by": "month"}
        )
        assert resp.status_code == 422

    async def test_from_window(self, client) -> None:
        ws = uuid4()
        early = datetime(2026, 1, 1, tzinfo=UTC)
        late = datetime(2026, 2, 1, tzinfo=UTC)
        await _record(client, ws, recorded_at=early.isoformat())
        await _record(client, ws, recorded_at=late.isoformat())
        resp = await client.get(
            f"/api/workspaces/{ws}/usage",
            params={"from": "2026-01-15T00:00:00Z"},
        )
        assert resp.json()["totals"]["sessions"] == 1

    async def test_naive_from_window(self, client) -> None:
        ws = uuid4()
        await _record(client, ws, recorded_at=datetime(2026, 1, 1, tzinfo=UTC).isoformat())
        await _record(client, ws, recorded_at=datetime(2026, 2, 1, tzinfo=UTC).isoformat())
        resp = await client.get(
            f"/api/workspaces/{ws}/usage",
            params={"from": "2026-01-15T00:00:00"},
        )
        assert resp.status_code == 200
        assert resp.json()["totals"]["sessions"] == 1

    async def test_naive_to_window(self, client) -> None:
        ws = uuid4()
        await _record(client, ws, recorded_at=datetime(2026, 1, 1, tzinfo=UTC).isoformat())
        await _record(client, ws, recorded_at=datetime(2026, 2, 1, tzinfo=UTC).isoformat())
        resp = await client.get(
            f"/api/workspaces/{ws}/usage",
            params={"to": "2026-01-15T00:00:00"},
        )
        assert resp.status_code == 200
        assert resp.json()["totals"]["sessions"] == 1

    async def test_date_only_from_window(self, client) -> None:
        ws = uuid4()
        await _record(client, ws, recorded_at=datetime(2026, 1, 1, tzinfo=UTC).isoformat())
        await _record(client, ws, recorded_at=datetime(2026, 2, 1, tzinfo=UTC).isoformat())
        resp = await client.get(
            f"/api/workspaces/{ws}/usage",
            params={"from": "2026-01-15"},
        )
        assert resp.status_code == 200
        assert resp.json()["totals"]["sessions"] == 1


class TestAgentUsage:
    async def test_unknown_agent_404(self, client) -> None:
        resp = await client.get(f"/api/agents/{uuid4()}/usage")
        assert resp.status_code == 404

    async def test_agent_usage_scoped(self, client) -> None:
        ws = uuid4()
        agent = await _create_agent(client, ws)
        await _record(
            client, ws, agent_id=agent["id"], backend="codex", tokens_in=10, tokens_out=0
        )
        await _record(client, ws, backend="claude", tokens_in=5, tokens_out=0)
        body = (await client.get(f"/api/agents/{agent['id']}/usage")).json()
        assert body["totals"]["tokens_in"] == 10
        assert body["totals"]["sessions"] == 1
