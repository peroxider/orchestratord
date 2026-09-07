"""Notification-channel integrations REST API (``docs/FEATURE_GAP_VS_MULTICA.md``
§7.5).

The persistent side of the Slack / Lark OAuth handshake: one integration per
workspace per provider, holding the inbound-webhook URL the adapters POST to.
Registration happens after the provider's OAuth / app-install redirect; the
channels (N bindings within an integration) are managed by the ``channels``
router, and delivery by ``orchestratord.notifications``.

``webhook_url`` is a credential; the owner/admin-facing surface returns it only
on registration, matching the one-time-plaintext convention of the tokens
router (§5.7.4). DingTalk / WeCom / Telegram are deferred behind the same
shape.
"""

from __future__ import annotations

import hashlib
import hmac
import os
from datetime import UTC, datetime
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel

from orchestratord.api.db import get_repositories
from orchestratord.api.routers.channels import dispatch_mention
from orchestratord.integrations.oauth import OAuthError, SlackOAuth, oauth_from_env
from orchestratord.db import models as orm
from orchestratord.db.repository import Repositories
from orchestratord.domain.integration import Integration

router = APIRouter(tags=["integrations"])


def _integration_payload(integration: orm.Integration) -> dict:
    return {
        "id": str(integration.id),
        "workspace_id": str(integration.workspace_id),
        "provider": integration.provider,
        "webhook_url": integration.webhook_url,
        "created_at": integration.created_at.isoformat(),
    }


class _IntegrationCreate(BaseModel):
    provider: str
    webhook_url: str


@router.post("/api/workspaces/{workspace_id}/integrations", status_code=201)
async def register_integration(
    workspace_id: UUID,
    body: _IntegrationCreate,
    repos: Repositories = Depends(get_repositories),
) -> dict:
    existing = await repos.integrations.by_workspace_provider(
        workspace_id, body.provider
    )
    if existing is not None:
        raise HTTPException(
            status_code=409, detail="integration already registered"
        )
    try:
        validated = Integration(
            id=uuid4(),
            workspace_id=workspace_id,
            provider=body.provider,
            webhook_url=body.webhook_url.strip(),
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    row = orm.Integration(
        id=validated.id,
        workspace_id=validated.workspace_id,
        provider=validated.provider,
        webhook_url=validated.webhook_url,
        created_at=validated.created_at,
    )
    await repos.integrations.add(row)
    return _integration_payload(row)


@router.get("/api/workspaces/{workspace_id}/integrations")
async def list_integrations(
    workspace_id: UUID,
    repos: Repositories = Depends(get_repositories),
) -> dict:
    rows = await repos.integrations.list_for_workspace(workspace_id)
    return {"integrations": [_integration_payload(i) for i in rows]}


@router.delete(
    "/api/workspaces/{workspace_id}/integrations/{integration_id}",
    status_code=204,
)
async def delete_integration(
    workspace_id: UUID,
    integration_id: UUID,
    repos: Repositories = Depends(get_repositories),
) -> None:
    integration = await repos.integrations.get(integration_id)
    if integration is None or integration.workspace_id != workspace_id:
        raise HTTPException(status_code=404, detail="integration not found")
    await repos.integrations.delete(integration)


# ---------------------------------------------------------------------------
# OAuth handshake (§6.3) + inbound webhook (§6.4)
# ---------------------------------------------------------------------------


def _state_secret() -> bytes:
    return os.environ.get("ORCHESTRATORD_SECRET_KEY", "orchestratord-dev-secret").encode()


def _sign_state(workspace_id: UUID) -> str:
    """CSRF-safe state: workspace id + truncated HMAC (stateless, cross-process)."""
    sig = hmac.new(
        _state_secret(), str(workspace_id).encode(), hashlib.sha256
    ).hexdigest()[:32]
    return f"{workspace_id}.{sig}"


def _verify_state(state: str) -> UUID | None:
    try:
        ws_part, sig = state.rsplit(".", 1)
    except ValueError:
        return None
    expected = hmac.new(
        _state_secret(), ws_part.encode(), hashlib.sha256
    ).hexdigest()[:32]
    if not hmac.compare_digest(sig, expected):
        return None
    try:
        return UUID(ws_part)
    except ValueError:
        return None


def _callback_uri(workspace_id: UUID, provider: str) -> str | None:
    base = os.environ.get("ORCHESTRATORD_PUBLIC_BASE_URL", "")
    if not base:
        return None  # provider falls back to its app-configured redirect URL
    return f"{base}/api/workspaces/{workspace_id}/integrations/{provider}/callback"


def get_slack_oauth() -> SlackOAuth | None:
    """Dependency seam: env-built client; tests override with MockTransport."""
    return oauth_from_env("slack")


@router.get("/api/workspaces/{workspace_id}/integrations/slack/authorize")
async def slack_authorize(
    workspace_id: UUID,
    repos: Repositories = Depends(get_repositories),
    oauth: SlackOAuth | None = Depends(get_slack_oauth),
) -> object:
    """Redirect to the Slack install page with a signed ``state`` (§6.3)."""
    workspace = await repos.workspaces.get(workspace_id)
    if workspace is None:
        raise HTTPException(status_code=404, detail="workspace not found")
    if oauth is None:
        raise HTTPException(
            status_code=503,
            detail="Slack OAuth not configured (set ORCHESTRATORD_SLACK_CLIENT_ID "
            "and ORCHESTRATORD_SLACK_CLIENT_SECRET)",
        )
    return RedirectResponse(
        oauth.authorize_url(
            _sign_state(workspace_id), redirect_uri=_callback_uri(workspace_id, "slack")
        ),
        status_code=302,
    )


@router.get("/api/workspaces/{workspace_id}/integrations/slack/callback")
async def slack_callback(
    workspace_id: UUID,
    code: str,
    state: str,
    repos: Repositories = Depends(get_repositories),
    oauth: SlackOAuth | None = Depends(get_slack_oauth),
) -> dict:
    """Exchange the install ``code`` and upsert the Slack integration (§6.3)."""
    if _verify_state(state) != workspace_id:
        raise HTTPException(status_code=403, detail="invalid oauth state")
    if oauth is None:
        raise HTTPException(
            status_code=503,
            detail="Slack OAuth not configured (set ORCHESTRATORD_SLACK_CLIENT_ID "
            "and ORCHESTRATORD_SLACK_CLIENT_SECRET)",
        )
    try:
        tokens = await oauth.exchange_code(code)
    except OAuthError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    webhook_url = (tokens.get("incoming_webhook") or {}).get("url")
    if not webhook_url:
        raise HTTPException(
            status_code=502,
            detail="oauth exchange returned no incoming webhook URL",
        )
    existing = await repos.integrations.by_workspace_provider(workspace_id, "slack")
    if existing is not None:
        existing.webhook_url = webhook_url
        integration = existing
    else:
        integration = orm.Integration(
            id=uuid4(),
            workspace_id=workspace_id,
            provider="slack",
            webhook_url=webhook_url,
            created_at=datetime.now(UTC),
        )
        await repos.integrations.add(integration)
    try:
        from orchestratord.api.realtime import get_broker

        await get_broker().publish(
            f"workspace.{workspace_id}",
            {"event": "integration_registered", "provider": "slack"},
        )
    except Exception:  # noqa: BLE001 — broker is a notification channel
        pass
    return {
        "registered": True,
        "provider": "slack",
        "workspace_id": str(workspace_id),
        "integration_id": str(integration.id),
    }


@router.post("/api/integrations/slack/events")
async def slack_events(
    request: Request,
    repos: Repositories = Depends(get_repositories),
) -> dict:
    """Slack Events API receiver (§6.4).

    URL verification echoes the ``challenge``; ``event_callback`` messages
    containing an ``@orchestratord <text>`` mention are dispatched through the
    shared channel-trigger logic (link or create an issue), resolving the
    channel by ``external_id`` (single-user mode). Always answers 200 for
    event payloads so Slack does not retry-loop on business-level misses.
    """
    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001 — malformed body is a handled miss
        return {"handled": False, "reason": "invalid json"}
    if not isinstance(payload, dict):
        return {"handled": False, "reason": "invalid payload type"}
    if payload.get("type") == "url_verification":
        return {"challenge": payload.get("challenge", "")}
    if payload.get("type") != "event_callback":
        return {"handled": False, "reason": "unsupported payload type"}
    event = payload.get("event") or {}
    text = (event.get("text") or "").strip()
    if not text.lower().startswith("@orchestratord"):
        return {"handled": False, "reason": "not an @orchestratord mention"}
    channel = await repos.channels.by_external_id(event.get("channel") or "")
    if channel is None:
        return {"handled": False, "reason": "unknown channel"}
    try:
        result = await dispatch_mention(repos, channel, text)
    except LookupError as exc:
        return {"handled": False, "reason": str(exc)}
    return {"handled": True, **result}
