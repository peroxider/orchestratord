"""GitHub App handshake contract tests (``.../FEATURE_GAP_VS_MULTICA.md`` §6.5).

Pins ``orchestratord.integrations.github_app.GitHubAppOAuth`` (install URL +
manifest ``code`` conversion over ``httpx.MockTransport`` — no network) and
the two router endpoints: ``GET .../vcs/github/authorize`` (302 + signed
state) and ``GET .../vcs/github/callback`` (manifest flow via ``code``, direct
install flow via ``installation_id`` + ``account_login``, cross-workspace 409).

Runs against the live ``orchestratord_test`` database and skips when Postgres
is unreachable.

Reference: docs/FEATURE_GAP_VS_MULTICA.md §6.5.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import httpx
import pytest

from orchestratord.api.routers.integrations import _sign_state, _verify_state
from orchestratord.api.routers.vcs import get_github_app_oauth
from orchestratord.integrations import GitHubAppOAuth, OAuthError
from orchestratord.integrations.github_app import github_app_from_env
from orchestratord.db import models as orm
from orchestratord.db.repository import Repositories

pytestmark = pytest.mark.database


@pytest.fixture
def install_gh(client):
    """Override ``get_github_app_oauth`` on the per-test app the client serves."""
    app_under_test = client._transport.app

    def _install(oauth) -> None:
        app_under_test.dependency_overrides[get_github_app_oauth] = lambda: oauth

    yield _install
    app_under_test.dependency_overrides.pop(get_github_app_oauth, None)


def _mock_gh(response_json: dict | None, captured: dict, status: int = 200):
    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["accept"] = request.headers.get("accept", "")
        return httpx.Response(status, json=response_json or {})

    return GitHubAppOAuth(
        "orchestratord",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )


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


class TestGitHubAppClient:
    def test_authorize_url_contains_slug_and_state(self) -> None:
        oauth = GitHubAppOAuth("orchestratord")
        url = oauth.authorize_url("st-1")
        assert url.startswith(
            "https://github.com/apps/orchestratord/installations/new"
        )
        assert "state=st-1" in url

    async def test_exchange_code_posts_conversion(self) -> None:
        captured: dict = {}
        oauth = _mock_gh(
            {
                "token": "ghs_x",
                "installation_id": 42,
                "account": {"login": "octocat"},
            },
            captured,
        )
        payload = await oauth.exchange_code("abc")
        assert "app-manifests/abc/conversions" in captured["url"]
        assert captured["accept"] == "application/json"
        assert payload["installation_id"] == 42
        assert payload["account"]["login"] == "octocat"

    async def test_exchange_code_raises_on_http_error(self) -> None:
        oauth = _mock_gh({}, {}, status=500)
        with pytest.raises(OAuthError):
            await oauth.exchange_code("bad")

    def test_installation_from_conversion_extracts(self) -> None:
        oauth = GitHubAppOAuth("orchestratord")
        details = oauth.installation_from_conversion(
            {"installation_id": "42", "account": {"login": "octocat"}}
        )
        assert details == {"installation_id": 42, "account_login": "octocat"}

    def test_installation_from_conversion_rejects_missing(self) -> None:
        oauth = GitHubAppOAuth("orchestratord")
        with pytest.raises(OAuthError):
            oauth.installation_from_conversion({"installation_id": 42})
        with pytest.raises(OAuthError):
            oauth.installation_from_conversion({"installation_id": 42, "account": {}})

    def test_from_env(self, monkeypatch) -> None:
        monkeypatch.setenv("ORCHESTRATORD_GITHUB_APP_SLUG", "orchestratord")
        assert isinstance(github_app_from_env(), GitHubAppOAuth)
        monkeypatch.delenv("ORCHESTRATORD_GITHUB_APP_SLUG")
        assert github_app_from_env() is None


class TestAuthorize:
    async def test_404_unknown_workspace(self, client, install_gh) -> None:
        install_gh(GitHubAppOAuth("orchestratord"))
        resp = await client.get(f"/api/workspaces/{uuid4()}/vcs/github/authorize")
        assert resp.status_code == 404

    async def test_503_unconfigured(self, client, db, install_gh) -> None:
        ws = await _seed_workspace(db)
        install_gh(None)
        resp = await client.get(f"/api/workspaces/{ws.id}/vcs/github/authorize")
        assert resp.status_code == 503

    async def test_redirects_with_signed_state(self, client, db, install_gh) -> None:
        ws = await _seed_workspace(db)
        install_gh(GitHubAppOAuth("orchestratord"))
        resp = await client.get(
            f"/api/workspaces/{ws.id}/vcs/github/authorize",
            follow_redirects=False,
        )
        assert resp.status_code == 302
        location = resp.headers["location"]
        assert "github.com/apps/orchestratord/installations/new" in location
        state = location.split("state=")[1]
        assert _verify_state(state) == ws.id


class TestCallback:
    async def test_403_bad_state(self, client, install_gh) -> None:
        install_gh(GitHubAppOAuth("orchestratord"))
        resp = await client.get(
            f"/api/workspaces/{uuid4()}/vcs/github/callback",
            params={"state": "tampered", "installation_id": 1, "account_login": "x"},
        )
        assert resp.status_code == 403

    async def test_422_without_code_or_installation(self, client, install_gh) -> None:
        ws = uuid4()
        install_gh(GitHubAppOAuth("orchestratord"))
        resp = await client.get(
            f"/api/workspaces/{ws}/vcs/github/callback",
            params={"state": _sign_state(ws)},
        )
        assert resp.status_code == 422

    async def test_manifest_flow_registers_installation(
        self, client, install_gh
    ) -> None:
        ws = uuid4()
        install_gh(
            _mock_gh(
                {
                    "token": "ghs_x",
                    "installation_id": 42,
                    "account": {"login": "octocat"},
                },
                {},
            )
        )
        resp = await client.get(
            f"/api/workspaces/{ws}/vcs/github/callback",
            params={"code": "abc", "state": _sign_state(ws)},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["registered"] is True
        assert resp.json()["installation_id"] == 42
        assert resp.json()["account_login"] == "octocat"

        listing = await client.get(f"/api/workspaces/{ws}/vcs/installations")
        rows = listing.json()["installations"]
        assert len(rows) == 1
        assert rows[0]["installation_id"] == 42
        assert rows[0]["account_login"] == "octocat"

    async def test_install_flow_registers_directly(self, client, install_gh) -> None:
        ws = uuid4()
        install_gh(GitHubAppOAuth("orchestratord"))
        resp = await client.get(
            f"/api/workspaces/{ws}/vcs/github/callback",
            params={
                "state": _sign_state(ws),
                "installation_id": 7,
                "account_login": "monalisa",
            },
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["installation_id"] == 7

        listing = await client.get(f"/api/workspaces/{ws}/vcs/installations")
        assert listing.json()["installations"][0]["account_login"] == "monalisa"

    async def test_cross_workspace_installation_409(
        self, client, db, install_gh
    ) -> None:
        ws_a = await _seed_workspace(db)
        ws_b = await _seed_workspace(db)
        install_gh(GitHubAppOAuth("orchestratord"))
        first = await client.get(
            f"/api/workspaces/{ws_a.id}/vcs/github/callback",
            params={
                "state": _sign_state(ws_a.id),
                "installation_id": 9,
                "account_login": "octocat",
            },
        )
        assert first.status_code == 200

        second = await client.get(
            f"/api/workspaces/{ws_b.id}/vcs/github/callback",
            params={
                "state": _sign_state(ws_b.id),
                "installation_id": 9,
                "account_login": "octocat",
            },
        )
        assert second.status_code == 409

    async def test_same_workspace_upserts(self, client, db, install_gh) -> None:
        ws = await _seed_workspace(db)
        install_gh(GitHubAppOAuth("orchestratord"))
        for login in ("octocat", "monalisa"):
            resp = await client.get(
                f"/api/workspaces/{ws.id}/vcs/github/callback",
                params={
                    "state": _sign_state(ws.id),
                    "installation_id": 9,
                    "account_login": login,
                },
            )
            assert resp.status_code == 200

        listing = await client.get(f"/api/workspaces/{ws.id}/vcs/installations")
        rows = listing.json()["installations"]
        assert len(rows) == 1
        assert rows[0]["account_login"] == "monalisa"
