"""Channels REST API contract (§7.5).

Pins the notification-channel surface: a workspace binds Slack / Lark
channels; an in-channel ``@orchestratord`` mention triggers issue dispatch and
session state transitions are pushed back to the channel. Tests run against
the live ``orchestratord_test`` database and skip when Postgres is unreachable.

Reference: docs/FEATURE_GAP_VS_MULTICA.md §7.5.
"""
from __future__ import annotations

from uuid import uuid4

import pytest

from orchestratord.api import notifications as api_notifications

pytestmark = pytest.mark.database


async def _create(client, ws, **overrides) -> dict:
    payload = {"provider": "slack", "name": "eng", "external_id": "C123"}
    payload.update(overrides)
    resp = await client.post(f"/api/workspaces/{ws}/channels", json=payload)
    assert resp.status_code == 201, resp.text
    return resp.json()


class TestCreate:
    async def test_create_returns_channel(self, client) -> None:
        ws = uuid4()
        body = await _create(client, ws)
        assert body["provider"] == "slack"
        assert body["name"] == "eng"
        assert body["external_id"] == "C123"
        assert body["workspace_id"] == str(ws)

    async def test_create_invalid_provider_422(self, client) -> None:
        ws = uuid4()
        resp = await client.post(
            f"/api/workspaces/{ws}/channels",
            json={"provider": "telegram", "name": "x", "external_id": "T1"},
        )
        assert resp.status_code == 422


class TestList:
    async def test_scoped_to_workspace(self, client) -> None:
        ws_a, ws_b = uuid4(), uuid4()
        await _create(client, ws_a)
        assert (await client.get(f"/api/workspaces/{ws_b}/channels")).json() == []


class TestDetail:
    async def test_cross_workspace_404(self, client) -> None:
        ws_a, ws_b = uuid4(), uuid4()
        channel = await _create(client, ws_a)
        resp = await client.get(f"/api/workspaces/{ws_b}/channels/{channel['id']}")
        assert resp.status_code == 404


class TestDelete:
    async def test_delete_then_404(self, client) -> None:
        ws = uuid4()
        channel = await _create(client, ws)
        resp = await client.delete(f"/api/workspaces/{ws}/channels/{channel['id']}")
        assert resp.status_code == 204
        assert (
            await client.get(f"/api/workspaces/{ws}/channels/{channel['id']}")
        ).status_code == 404


class TestTrigger:
    async def test_trigger_with_existing_issue(self, client) -> None:
        ws = uuid4()
        issue = (
            await client.post(
                f"/api/workspaces/{ws}/issues", json={"title": "Bug"}
            )
        ).json()
        channel = await _create(client, ws)
        resp = await client.post(
            f"/api/channels/{channel['id']}/trigger",
            json={"text": f"@orchestratord {issue['id']}"},
        )
        assert resp.status_code == 202
        assert resp.json()["issue_id"] == issue["id"]
        assert resp.json()["created"] is False

    async def test_trigger_unknown_issue_404(self, client) -> None:
        ws = uuid4()
        channel = await _create(client, ws)
        resp = await client.post(
            f"/api/channels/{channel['id']}/trigger",
            json={"text": f"@orchestratord {uuid4()}"},
        )
        assert resp.status_code == 404

    async def test_trigger_natural_language_creates_issue(self, client) -> None:
        ws = uuid4()
        channel = await _create(client, ws)
        resp = await client.post(
            f"/api/channels/{channel['id']}/trigger",
            json={"text": "@orchestratord fix the login bug"},
        )
        assert resp.status_code == 202
        body = resp.json()
        assert body["created"] is True
        assert body["issue_id"] is not None
        listed = (await client.get(f"/api/workspaces/{ws}/issues")).json()
        assert [i["title"] for i in listed] == ["fix the login bug"]

    async def test_trigger_unknown_channel_404(self, client) -> None:
        resp = await client.post(
            f"/api/channels/{uuid4()}/trigger", json={"text": "@orchestratord hi"}
        )
        assert resp.status_code == 404


class _RecordingService:
    def __init__(self) -> None:
        self.deliveries: list[dict] = []

    async def deliver(self, *, provider: str, webhook_url: str, text: str) -> bool:
        self.deliveries.append(
            {"provider": provider, "webhook_url": webhook_url, "text": text}
        )
        return True


class TestPush:
    async def test_push_delivers(self, client, monkeypatch) -> None:
        ws = uuid4()
        channel = await _create(client, ws)
        await client.post(
            f"/api/workspaces/{ws}/integrations",
            json={
                "provider": "slack",
                "webhook_url": "https://hooks.slack.com/T/B/X",
            },
        )
        fake = _RecordingService()
        monkeypatch.setattr(api_notifications, "_default", fake)
        session_id = uuid4()
        resp = await client.post(
            f"/api/channels/{channel['id']}/push",
            json={"session_id": str(session_id), "status": "pending_review"},
        )
        assert resp.status_code == 202
        body = resp.json()
        assert body["pushed"] is True
        assert body["status"] == "pending_review"
        assert fake.deliveries == [
            {
                "provider": "slack",
                "webhook_url": "https://hooks.slack.com/T/B/X",
                "text": f"session {session_id} -> pending_review",
            }
        ]

    async def test_push_without_integration_409(self, client) -> None:
        ws = uuid4()
        channel = await _create(client, ws)
        resp = await client.post(
            f"/api/channels/{channel['id']}/push",
            json={"session_id": str(uuid4()), "status": "pending_review"},
        )
        assert resp.status_code == 409

    async def test_push_unknown_channel_404(self, client) -> None:
        resp = await client.post(
            f"/api/channels/{uuid4()}/push",
            json={"session_id": str(uuid4()), "status": "pending_review"},
        )
        assert resp.status_code == 404
