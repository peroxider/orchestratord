"""Slack/Lark OAuth handshake + inbound webhook contract tests (§6.3/§6.4).

Pins ``orchestratord.integrations.oauth`` (``authorize_url`` / ``exchange_code``
/ ``refresh_token`` with ``httpx.MockTransport`` — no network) and the three
router endpoints: ``GET .../integrations/slack/authorize`` (302 + signed
state), ``GET .../integrations/slack/callback`` (state verify, exchange,
integration upsert), and ``POST /api/integrations/slack/events`` (URL
verification + ``@orchestratord`` mention dispatch via ``external_id``).

Runs against the live ``orchestratord_test`` database and skips when Postgres
is unreachable.

Reference: docs/FEATURE_GAP_VS_MULTICA.md §6.3, §6.4, §6.6.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from uuid import uuid4

import httpx
import pytest

from orchestratord.api.routers.integrations import (
    _sign_state,
    _verify_state,
    get_slack_oauth,
)
from orchestratord.integrations import LarkOAuth, OAuthError, SlackOAuth
from orchestratord.integrations.oauth import oauth_from_env
from orchestratord.db import models as orm
from orchestratord.db.repository import Repositories

pytestmark = pytest.mark.database


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def install_oauth(client):
    """Override ``get_slack_oauth`` on the per-test app the client serves.

    ``conftest.client`` builds a fresh app per test (so overrides never leak),
    so the override must target that instance, not the module-level app.
    """
    app_under_test = client._transport.app

    def _install(oauth) -> None:
        app_under_test.dependency_overrides[get_slack_oauth] = lambda: oauth

    yield _install
    app_under_test.dependency_overrides.pop(get_slack_oauth, None)


def _mock_oauth(response_json: dict, captured: dict, provider: str = "slack"):
    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = request.content.decode()
        return httpx.Response(200, json=response_json)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    if provider == "slack":
        return SlackOAuth("cid", "csec", client=client)
    return LarkOAuth("aid", "asec", client=client)


async def _seed_workspace(db, **overrides) -> orm.Workspace:
    defaults = {
        "id": uuid4(),
        "slug": f"ws-{uuid4().hex[:8]}",
        "name": "Test Workspace",
        "created_at": datetime.now(UTC),
    }
    defaults.update(overrides)
    ws = orm.Workspace(**defaults)
    await Repositories(db).workspaces.add(ws)
    await db.commit()
    return ws


async def _seed_channel(client, ws: str, external_id: str) -> dict:
    resp = await client.post(
        f"/api/workspaces/{ws}/channels",
        json={"provider": "slack", "name": "general", "external_id": external_id},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


# ---------------------------------------------------------------------------
# OAuth clients (§6.3)
# ---------------------------------------------------------------------------


class TestOAuthClients:
    def test_slack_authorize_url_contains_client_and_state(self) -> None:
        oauth = SlackOAuth("cid", "csec")
        url = oauth.authorize_url("st-1", "https://example.test/cb")
        assert url.startswith("https://slack.com/oauth/v2/authorize?")
        assert "client_id=cid" in url
        assert "state=st-1" in url
        assert "redirect_uri=https" in url

    async def test_slack_exchange_posts_form(self) -> None:
        captured: dict = {}
        oauth = _mock_oauth(
            {
                "ok": True,
                "access_token": "xoxb-1",
                "incoming_webhook": {"url": "https://hooks.slack.com/services/x"},
            },
            captured,
        )
        tokens = await oauth.exchange_code("abc")
        assert "oauth.v2.access" in captured["url"]
        assert "client_id=cid" in captured["body"]
        assert "client_secret=csec" in captured["body"]
        assert "code=abc" in captured["body"]
        assert tokens["incoming_webhook"]["url"] == "https://hooks.slack.com/services/x"

    async def test_slack_exchange_raises_on_ok_false(self) -> None:
        oauth = _mock_oauth({"ok": False, "error": "invalid_code"}, {})
        with pytest.raises(OAuthError, match="invalid_code"):
            await oauth.exchange_code("bad")

    async def test_slack_refresh_posts_to_rotate(self) -> None:
        captured: dict = {}
        oauth = _mock_oauth({"ok": True, "access_token": "xoxb-2"}, captured)
        tokens = await oauth.refresh_token("rt-1")
        assert "auth.rotate" in captured["url"]
        assert "refresh_token=rt-1" in captured["body"]
        assert tokens["access_token"] == "xoxb-2"

    def test_lark_authorize_url_contains_app_id(self) -> None:
        oauth = LarkOAuth("aid", "asec")
        url = oauth.authorize_url("st-2", "https://example.test/cb")
        assert url.startswith("https://open.feishu.cn/open-apis/authen/v1/authorize?")
        assert "app_id=aid" in url
        assert "state=st-2" in url

    async def test_lark_exchange_posts_json(self) -> None:
        captured: dict = {}
        oauth = _mock_oauth(
            {"code": 0, "access_token": "u-1", "refresh_token": "r-1"},
            captured,
            provider="lark",
        )
        tokens = await oauth.exchange_code("abc")
        payload = json.loads(captured["body"])
        assert payload["grant_type"] == "authorization_code"
        assert payload["client_id"] == "aid"
        assert payload["code"] == "abc"
        assert tokens["access_token"] == "u-1"

    async def test_lark_exchange_raises_on_error_code(self) -> None:
        oauth = _mock_oauth({"code": 99991663, "msg": "bad"}, {}, provider="lark")
        with pytest.raises(OAuthError):
            await oauth.exchange_code("bad")

    async def test_lark_refresh_grant_type(self) -> None:
        captured: dict = {}
        oauth = _mock_oauth(
            {"code": 0, "access_token": "u-2"}, captured, provider="lark"
        )
        await oauth.refresh_token("rt-2")
        payload = json.loads(captured["body"])
        assert payload["grant_type"] == "refresh_token"
        assert payload["refresh_token"] == "rt-2"

    def test_oauth_from_env_reads_vars(self, monkeypatch) -> None:
        monkeypatch.setenv("ORCHESTRATORD_SLACK_CLIENT_ID", "cid")
        monkeypatch.setenv("ORCHESTRATORD_SLACK_CLIENT_SECRET", "csec")
        oauth = oauth_from_env("slack")
        assert isinstance(oauth, SlackOAuth)
        assert oauth.client_id == "cid"

        monkeypatch.setenv("ORCHESTRATORD_LARK_APP_ID", "aid")
        monkeypatch.setenv("ORCHESTRATORD_LARK_APP_SECRET", "asec")
        assert isinstance(oauth_from_env("lark"), LarkOAuth)

    def test_oauth_from_env_unconfigured_returns_none(self, monkeypatch) -> None:
        for var in (
            "ORCHESTRATORD_SLACK_CLIENT_ID",
            "ORCHESTRATORD_SLACK_CLIENT_SECRET",
            "ORCHESTRATORD_LARK_APP_ID",
            "ORCHESTRATORD_LARK_APP_SECRET",
        ):
            monkeypatch.delenv(var, raising=False)
        assert oauth_from_env("slack") is None
        assert oauth_from_env("lark") is None


# ---------------------------------------------------------------------------
# State signing
# ---------------------------------------------------------------------------


class TestState:
    def test_sign_verify_roundtrip(self) -> None:
        ws = uuid4()
        assert _verify_state(_sign_state(ws)) == ws

    def test_tampered_state_rejected(self) -> None:
        signed = _sign_state(uuid4())
        assert _verify_state(signed[:-2] + "zz") is None
        assert _verify_state("not-a-state") is None


# ---------------------------------------------------------------------------
# Authorize endpoint (§6.3)
# ---------------------------------------------------------------------------


class TestAuthorizeEndpoint:
    async def test_404_unknown_workspace(self, client, install_oauth) -> None:
        install_oauth(SlackOAuth("cid", "csec"))
        resp = await client.get(
            f"/api/workspaces/{uuid4()}/integrations/slack/authorize"
        )
        assert resp.status_code == 404

    async def test_503_when_unconfigured(self, client, db, install_oauth) -> None:
        ws = await _seed_workspace(db)
        install_oauth(None)
        resp = await client.get(
            f"/api/workspaces/{ws.id}/integrations/slack/authorize"
        )
        assert resp.status_code == 503

    async def test_redirects_with_signed_state(self, client, db, install_oauth) -> None:
        ws = await _seed_workspace(db)
        install_oauth(SlackOAuth("cid", "csec"))
        resp = await client.get(
            f"/api/workspaces/{ws.id}/integrations/slack/authorize",
            follow_redirects=False,
        )
        assert resp.status_code == 302
        location = resp.headers["location"]
        assert location.startswith("https://slack.com/oauth/v2/authorize?")
        assert "client_id=cid" in location
        state = location.split("state=")[1].split("&")[0]
        assert _verify_state(state) == ws.id


# ---------------------------------------------------------------------------
# Callback endpoint (§6.3)
# ---------------------------------------------------------------------------


def _ok_tokens() -> dict:
    return {
        "ok": True,
        "access_token": "xoxb-1",
        "incoming_webhook": {"url": "https://hooks.slack.com/services/T/B/x"},
    }


class TestCallbackEndpoint:
    async def test_403_on_bad_state(self, client, install_oauth) -> None:
        install_oauth(_mock_oauth(_ok_tokens(), {}))
        resp = await client.get(
            f"/api/workspaces/{uuid4()}/integrations/slack/callback",
            params={"code": "abc", "state": "tampered"},
        )
        assert resp.status_code == 403

    async def test_registers_integration(self, client, install_oauth) -> None:
        ws = uuid4()
        install_oauth(_mock_oauth(_ok_tokens(), {}))
        resp = await client.get(
            f"/api/workspaces/{ws}/integrations/slack/callback",
            params={"code": "abc", "state": _sign_state(ws)},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["registered"] is True
        assert resp.json()["provider"] == "slack"

        listing = await client.get(f"/api/workspaces/{ws}/integrations")
        rows = listing.json()["integrations"]
        assert len(rows) == 1
        assert rows[0]["provider"] == "slack"
        assert rows[0]["webhook_url"] == "https://hooks.slack.com/services/T/B/x"

    async def test_upserts_existing_integration(self, client, install_oauth) -> None:
        ws = uuid4()
        captured: dict = {}
        install_oauth(_mock_oauth(_ok_tokens(), {}))
        first = await client.get(
            f"/api/workspaces/{ws}/integrations/slack/callback",
            params={"code": "abc", "state": _sign_state(ws)},
        )
        assert first.status_code == 200

        updated = dict(_ok_tokens())
        updated["incoming_webhook"] = {"url": "https://hooks.slack.com/services/T/B/y"}
        install_oauth(_mock_oauth(updated, captured))
        second = await client.get(
            f"/api/workspaces/{ws}/integrations/slack/callback",
            params={"code": "def", "state": _sign_state(ws)},
        )
        assert second.status_code == 200

        listing = await client.get(f"/api/workspaces/{ws}/integrations")
        rows = listing.json()["integrations"]
        assert len(rows) == 1
        assert rows[0]["webhook_url"] == "https://hooks.slack.com/services/T/B/y"

    async def test_502_on_provider_error(self, client, install_oauth) -> None:
        ws = uuid4()
        install_oauth(_mock_oauth({"ok": False, "error": "invalid_code"}, {}))
        resp = await client.get(
            f"/api/workspaces/{ws}/integrations/slack/callback",
            params={"code": "abc", "state": _sign_state(ws)},
        )
        assert resp.status_code == 502

    async def test_502_without_incoming_webhook(self, client, install_oauth) -> None:
        ws = uuid4()
        install_oauth(_mock_oauth({"ok": True, "access_token": "xoxb-1"}, {}))
        resp = await client.get(
            f"/api/workspaces/{ws}/integrations/slack/callback",
            params={"code": "abc", "state": _sign_state(ws)},
        )
        assert resp.status_code == 502

    async def test_state_for_other_workspace_rejected(
        self, client, install_oauth
    ) -> None:
        ws = uuid4()
        install_oauth(_mock_oauth(_ok_tokens(), {}))
        resp = await client.get(
            f"/api/workspaces/{ws}/integrations/slack/callback",
            params={"code": "abc", "state": _sign_state(uuid4())},
        )
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Inbound webhook (§6.4)
# ---------------------------------------------------------------------------


def _event(channel: str, text: str) -> dict:
    return {
        "type": "event_callback",
        "event": {"type": "app_mention", "channel": channel, "text": text},
    }


class TestSlackEvents:
    async def test_url_verification_echoes_challenge(self, client) -> None:
        resp = await client.post(
            "/api/integrations/slack/events",
            json={"type": "url_verification", "challenge": "ch-123"},
        )
        assert resp.status_code == 200
        assert resp.json() == {"challenge": "ch-123"}

    async def test_unsupported_payload_type(self, client) -> None:
        resp = await client.post(
            "/api/integrations/slack/events", json={"type": "other"}
        )
        assert resp.status_code == 200
        assert resp.json()["handled"] is False

    async def test_non_dict_json_handled_not_500(self, client) -> None:
        """Valid-JSON-but-not-an-object must not crash (Slack retries on 5xx)."""
        for body in ([1, 2, 3], "just a string", 42):
            resp = await client.post("/api/integrations/slack/events", json=body)
            assert resp.status_code == 200, f"{body!r} -> {resp.status_code}"
            assert resp.json()["handled"] is False

    async def test_non_mention_message_ignored(self, client) -> None:
        resp = await client.post(
            "/api/integrations/slack/events", json=_event("C123", "hello world")
        )
        assert resp.json()["handled"] is False

    async def test_unknown_channel_not_handled(self, client) -> None:
        resp = await client.post(
            "/api/integrations/slack/events",
            json=_event("C-none", "@orchestratord ship the docs"),
        )
        assert resp.status_code == 200
        assert resp.json()["handled"] is False
        assert resp.json()["reason"] == "unknown channel"

    async def test_mention_creates_issue(self, client) -> None:
        ws = str(uuid4())
        await _seed_channel(client, ws, "C123")
        resp = await client.post(
            "/api/integrations/slack/events",
            json=_event("C123", "@orchestratord ship the docs"),
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["handled"] is True
        assert body["created"] is True

        issues = await client.get(f"/api/workspaces/{ws}/issues")
        assert [i["title"] for i in issues.json()] == ["ship the docs"]

    async def test_mention_links_existing_issue(self, client) -> None:
        ws = str(uuid4())
        await _seed_channel(client, ws, "C123")
        created = await client.post(
            f"/api/workspaces/{ws}/issues", json={"title": "existing"}
        )
        issue_id = created.json()["id"]

        resp = await client.post(
            "/api/integrations/slack/events",
            json=_event("C123", f"@orchestratord please {issue_id}"),
        )
        body = resp.json()
        assert body["handled"] is True
        assert body["created"] is False
        assert body["issue_id"] == issue_id
