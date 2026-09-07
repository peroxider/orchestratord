"""Channels REST API (``docs/FEATURE_GAP_VS_MULTICA.md`` §7.5).

Notification-channel bindings (Slack / Lark). A workspace binds N channels;
an in-channel ``@orchestratord <issue-id or natural language>`` mention
triggers issue dispatch (linking an existing issue or creating a new one), and
session state transitions are pushed back to the channel via the workspace's
integration (``orchestratord.notifications``). DingTalk / WeCom / Telegram are
deferred behind the same adapter interface (``apps/web/app/{dingtalk,wecom,telegram}``).
"""

from __future__ import annotations

from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from orchestratord.api.db import get_repositories
from orchestratord.api.notifications import get_notification_service
from orchestratord.db import models as orm
from orchestratord.db.repository import Repositories
from orchestratord.domain.channel import Channel
from orchestratord.domain.issue import Issue
from orchestratord.notifications import NotificationService

router = APIRouter(tags=["channels"])


def _channel_payload(channel: orm.Channel) -> dict:
    return {
        "id": str(channel.id),
        "workspace_id": str(channel.workspace_id),
        "provider": channel.provider,
        "name": channel.name,
        "external_id": channel.external_id,
        "created_at": channel.created_at.isoformat() if channel.created_at else None,
    }


async def _channel_or_404(
    repos: Repositories, workspace_id: UUID, channel_id: UUID
) -> orm.Channel:
    channel = await repos.channels.get(channel_id)
    if channel is None or channel.workspace_id != workspace_id:
        raise HTTPException(status_code=404, detail="channel not found")
    return channel


async def _channel_by_id_or_404(
    repos: Repositories, channel_id: UUID
) -> orm.Channel:
    channel = await repos.channels.get(channel_id)
    if channel is None:
        raise HTTPException(status_code=404, detail="channel not found")
    return channel


def _parse_issue_mention(text: str) -> UUID | None:
    """Extract a trailing issue id (UUID) from an ``@orchestratord`` mention.

    Returns ``None`` when the mention is natural language (no UUID token), in
    which case the caller treats it as a new-issue request.
    """
    for token in reversed(text.split()):
        try:
            return UUID(token)
        except ValueError:
            continue
    return None


def _mention_text(text: str) -> str:
    """Return the mention's payload with a leading ``@orchestratord`` stripped."""
    stripped = text.strip()
    if stripped.lower().startswith("@orchestratord"):
        return stripped[len("@orchestratord") :].strip()
    return stripped


async def dispatch_mention(
    repos: Repositories, channel: orm.Channel, text: str
) -> dict:
    """Shared ``@orchestratord <text>`` dispatch (link or create an issue).

    Raises ``LookupError`` when a trailing UUID names a missing or
    cross-workspace issue; callers map that to their transport (HTTP 404 for
    the REST trigger, ``handled: false`` for the inbound webhook §6.4).
    """
    issue_id = _parse_issue_mention(text)
    if issue_id is not None:
        issue = await repos.issues.get(issue_id)
        if issue is None or issue.workspace_id != channel.workspace_id:
            raise LookupError("issue not found")
        return {"issue_id": str(issue_id), "created": False}
    title = _mention_text(text)
    issue = Issue(
        id=uuid4(),
        workspace_id=channel.workspace_id,
        title=title,
        description=title,
        status="pending",
    )
    await repos.issues.add(
        orm.Issue(
            id=issue.id,
            workspace_id=issue.workspace_id,
            title=issue.title,
            description=issue.description,
            status=issue.status,
            assignee_type=None,
            assignee_id=None,
            created_at=issue.created_at,
        )
    )
    return {"issue_id": str(issue.id), "created": True}


class _ChannelCreate(BaseModel):
    provider: str
    name: str
    external_id: str


class _TriggerIn(BaseModel):
    text: str


class _PushIn(BaseModel):
    session_id: UUID
    status: str


@router.get("/api/workspaces/{workspace_id}/channels")
async def list_channels(
    workspace_id: UUID, repos: Repositories = Depends(get_repositories)
) -> list[dict]:
    channels = await repos.channels.list_for_workspace(workspace_id)
    return [_channel_payload(c) for c in channels]


@router.post("/api/workspaces/{workspace_id}/channels", status_code=201)
async def create_channel(
    workspace_id: UUID,
    body: _ChannelCreate,
    repos: Repositories = Depends(get_repositories),
) -> dict:
    try:
        validated = Channel(
            id=uuid4(),
            workspace_id=workspace_id,
            provider=body.provider,
            name=body.name.strip(),
            external_id=body.external_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    channel = orm.Channel(
        id=validated.id,
        workspace_id=workspace_id,
        provider=validated.provider,
        name=validated.name,
        external_id=validated.external_id,
        created_at=validated.created_at,
    )
    await repos.channels.add(channel)
    return _channel_payload(channel)


@router.get("/api/workspaces/{workspace_id}/channels/{channel_id}")
async def get_channel(
    workspace_id: UUID,
    channel_id: UUID,
    repos: Repositories = Depends(get_repositories),
) -> dict:
    channel = await _channel_or_404(repos, workspace_id, channel_id)
    return _channel_payload(channel)


@router.delete("/api/workspaces/{workspace_id}/channels/{channel_id}", status_code=204)
async def delete_channel(
    workspace_id: UUID,
    channel_id: UUID,
    repos: Repositories = Depends(get_repositories),
) -> None:
    channel = await _channel_or_404(repos, workspace_id, channel_id)
    await repos.channels.delete(channel)


@router.post("/api/channels/{channel_id}/trigger", status_code=202)
async def trigger(
    channel_id: UUID,
    body: _TriggerIn,
    repos: Repositories = Depends(get_repositories),
) -> dict:
    channel = await _channel_by_id_or_404(repos, channel_id)
    try:
        result = await dispatch_mention(repos, channel, body.text)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"dispatched": True, "channel_id": str(channel_id), **result}


@router.post("/api/channels/{channel_id}/push", status_code=202)
async def push(
    channel_id: UUID,
    body: _PushIn,
    repos: Repositories = Depends(get_repositories),
    service: NotificationService = Depends(get_notification_service),
) -> dict:
    channel = await _channel_by_id_or_404(repos, channel_id)
    integration = await repos.integrations.by_workspace_provider(
        channel.workspace_id, channel.provider
    )
    if integration is None:
        raise HTTPException(
            status_code=409, detail="no integration configured for provider"
        )
    text = f"session {body.session_id} -> {body.status}"
    delivered = await service.deliver(
        provider=integration.provider,
        webhook_url=integration.webhook_url,
        text=text,
    )
    return {
        "pushed": delivered,
        "channel_id": str(channel_id),
        "session_id": str(body.session_id),
        "status": body.status,
    }
