"""OAuth 2.0 authorization-code handshakes for Slack / Lark (§6.3).

Each provider client implements ``authorize_url()`` / ``exchange_code()`` /
``refresh_token()``; credentials come from the environment
(``ORCHESTRATORD_SLACK_CLIENT_ID`` / ``_SECRET``,
``ORCHESTRATORD_LARK_APP_ID`` / ``_SECRET``) via :func:`oauth_from_env`,
which returns ``None`` when the provider is unconfigured so the router can
answer 503 instead of leaking a half-initialized handshake.

The optional ``httpx.AsyncClient`` constructor parameter is the test seam
(mirrors ``orchestratord.notifications.adapters``): tests inject a
``MockTransport``-backed client instead of hitting the provider.
"""

from __future__ import annotations

import os
from typing import Any

import httpx

SLACK_AUTHORIZE_URL = "https://slack.com/oauth/v2/authorize"
SLACK_TOKEN_URL = "https://slack.com/api/oauth.v2.access"
SLACK_ROTATE_URL = "https://slack.com/api/auth.rotate"
# bot scopes: chat:write for the notification push, app_mentions:read +
# channels:read for the inbound webhook (§6.4).
SLACK_BOT_SCOPES = "chat:write,app_mentions:read,channels:read"

LARK_AUTHORIZE_URL = "https://open.feishu.cn/open-apis/authen/v1/authorize"
LARK_TOKEN_URL = "https://open.feishu.cn/open-apis/authen/v2/oauth/token"


class OAuthError(RuntimeError):
    """Provider rejected the handshake (HTTP error or app-level failure)."""


class SlackOAuth:
    """Slack app-install handshake (``oauth.v2.access``)."""

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.client_id = client_id
        self.client_secret = client_secret
        self._client = client

    async def _post(self, url: str, **kwargs: Any) -> dict:
        client = self._client or httpx.AsyncClient()
        try:
            resp = await client.post(url, **kwargs)
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise OAuthError(f"slack token endpoint failed: {exc}") from exc
        finally:
            if self._client is None:
                await client.aclose()
        data = resp.json()
        # Slack wraps failures in HTTP 200 with ``ok: false``.
        if not data.get("ok", False):
            raise OAuthError(f"slack oauth error: {data.get('error', 'unknown')}")
        return data

    def authorize_url(self, state: str, redirect_uri: str | None = None) -> str:
        params: dict[str, str] = {
            "client_id": self.client_id,
            "scope": SLACK_BOT_SCOPES,
            "state": state,
        }
        if redirect_uri:
            params["redirect_uri"] = redirect_uri
        return f"{SLACK_AUTHORIZE_URL}?{httpx.QueryParams(params)}"

    async def exchange_code(
        self, code: str, redirect_uri: str | None = None
    ) -> dict:
        form: dict[str, str] = {
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "code": code,
        }
        if redirect_uri:
            form["redirect_uri"] = redirect_uri
        return await self._post(SLACK_TOKEN_URL, data=form)

    async def refresh_token(self, refresh_token: str) -> dict:
        """Slack token rotation (``auth.rotate``) for refreshing apps."""
        return await self._post(SLACK_ROTATE_URL, data={"refresh_token": refresh_token})


class LarkOAuth:
    """Lark / Feishu OIDC handshake (``authen.v2.oauth.token``)."""

    def __init__(
        self,
        app_id: str,
        app_secret: str,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.app_id = app_id
        self.app_secret = app_secret
        self._client = client

    async def _post(self, payload: dict) -> dict:
        client = self._client or httpx.AsyncClient()
        try:
            resp = await client.post(LARK_TOKEN_URL, json=payload)
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise OAuthError(f"lark token endpoint failed: {exc}") from exc
        finally:
            if self._client is None:
                await client.aclose()
        data = resp.json()
        if data.get("code") not in (0, None):
            raise OAuthError(f"lark oauth error: {data.get('code')}")
        return data

    def authorize_url(self, state: str, redirect_uri: str | None = None) -> str:
        params: dict[str, str] = {
            "app_id": self.app_id,
            "state": state,
        }
        if redirect_uri:
            params["redirect_uri"] = redirect_uri
        return f"{LARK_AUTHORIZE_URL}?{httpx.QueryParams(params)}"

    async def exchange_code(
        self, code: str, redirect_uri: str | None = None
    ) -> dict:
        payload: dict[str, str] = {
            "grant_type": "authorization_code",
            "client_id": self.app_id,
            "client_secret": self.app_secret,
            "code": code,
        }
        if redirect_uri:
            payload["redirect_uri"] = redirect_uri
        return await self._post(payload)

    async def refresh_token(self, refresh_token: str) -> dict:
        return await self._post(
            {
                "grant_type": "refresh_token",
                "client_id": self.app_id,
                "client_secret": self.app_secret,
                "refresh_token": refresh_token,
            }
        )


def oauth_from_env(provider: str) -> SlackOAuth | LarkOAuth | None:
    """Build the provider client from env; ``None`` when unconfigured."""
    if provider == "slack":
        client_id = os.environ.get("ORCHESTRATORD_SLACK_CLIENT_ID", "")
        client_secret = os.environ.get("ORCHESTRATORD_SLACK_CLIENT_SECRET", "")
        if client_id and client_secret:
            return SlackOAuth(client_id, client_secret)
        return None
    if provider == "lark":
        app_id = os.environ.get("ORCHESTRATORD_LARK_APP_ID", "")
        app_secret = os.environ.get("ORCHESTRATORD_LARK_APP_SECRET", "")
        if app_id and app_secret:
            return LarkOAuth(app_id, app_secret)
        return None
    raise OAuthError(f"unsupported oauth provider {provider!r}")


__all__ = ["LarkOAuth", "OAuthError", "SlackOAuth", "oauth_from_env"]
