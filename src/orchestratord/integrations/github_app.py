"""GitHub App handshake (§6.5) — isomorphic to the §6.3 Slack/Lark clients.

Two GitHub install shapes share one client:

* **Manifest flow** — when the app was created from a manifest, the callback
  carries a one-time ``code`` which :meth:`exchange_code` converts via
  ``POST /app-manifests/{code}/conversions``. The conversion payload carries
  the ``installation_id`` and ``account.login`` the installations table needs.
* **Pre-existing app** — the callback carries ``installation_id`` directly
  (no exchange); the router registers it with an explicit ``account_login``.

The conversion's app token is intentionally not persisted: the v2 persistence
model (``installations`` table) has no token column, and webhook-driven PR
sync (§6.5) does not need it. Credentials come from
``ORCHESTRATORD_GITHUB_APP_SLUG`` via :func:`github_app_from_env`.
"""

from __future__ import annotations

import os
from typing import Any

import httpx

from orchestratord.integrations.oauth import OAuthError

GITHUB_INSTALL_URL = "https://github.com/apps/{slug}/installations/new"
GITHUB_MANIFEST_CONVERSION_URL = (
    "https://api.github.com/app-manifests/{code}/conversions"
)


class GitHubAppOAuth:
    """GitHub App install handshake (manifest conversion)."""

    def __init__(
        self,
        app_slug: str,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.app_slug = app_slug
        self._client = client

    def authorize_url(self, state: str) -> str:
        return f"{GITHUB_INSTALL_URL.format(slug=self.app_slug)}?state={state}"

    async def exchange_code(self, code: str) -> dict:
        """Convert a manifest-flow ``code`` into installation details."""
        client = self._client or httpx.AsyncClient()
        try:
            resp = await client.post(
                GITHUB_MANIFEST_CONVERSION_URL.format(code=code),
                headers={"Accept": "application/json"},
            )
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise OAuthError(f"github conversion failed: {exc}") from exc
        finally:
            if self._client is None:
                await client.aclose()
        return resp.json()

    def installation_from_conversion(self, conversion: dict[str, Any]) -> dict:
        """Extract ``(installation_id, account_login)`` from a conversion."""
        installation_id = conversion.get("installation_id")
        login = (conversion.get("account") or {}).get("login")
        if installation_id is None or not login:
            raise OAuthError("conversion payload missing installation_id or account")
        return {"installation_id": int(installation_id), "account_login": login}


def github_app_from_env() -> GitHubAppOAuth | None:
    """Build the client from env; ``None`` when unconfigured."""
    slug = os.environ.get("ORCHESTRATORD_GITHUB_APP_SLUG", "")
    if slug:
        return GitHubAppOAuth(slug)
    return None


__all__ = ["GitHubAppOAuth", "OAuthError", "github_app_from_env"]
