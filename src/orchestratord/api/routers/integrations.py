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

from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from orchestratord.api.db import get_repositories
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
