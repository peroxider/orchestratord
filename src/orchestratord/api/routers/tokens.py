"""API tokens REST API (``docs/FEATURE_GAP_VS_MULTICA.md`` §5.7.4).

Multica-style API tokens grant scoped workspace access to the Web client /
automation. Creation issues a one-time plaintext token (only its SHA-256 hash
is stored on the entity), so list/detail never expose the token or its hash.
Revoke deletes the token, barring further use.

Reference: docs/FEATURE_GAP_VS_MULTICA.md §5.7.4, §6.1.1.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from orchestratord.api.db import get_repositories
from orchestratord.db import models as orm
from orchestratord.db.repository import Repositories
from orchestratord.domain.auth_token import AuthToken, issue_api_token

router = APIRouter(tags=["tokens"])


def _token_payload(token: orm.AuthToken) -> dict:
    return {
        "id": str(token.id),
        "workspace_id": str(token.workspace_id),
        "name": token.name,
        "scopes": list(token.scopes),
        "expires_at": token.expires_at.isoformat() if token.expires_at else None,
        "created_at": token.created_at.isoformat() if token.created_at else None,
    }


async def _token_or_404(
    repos: Repositories, workspace_id: UUID, token_id: UUID
) -> orm.AuthToken:
    token = await repos.auth_tokens.get(token_id)
    if token is None or token.workspace_id != workspace_id:
        raise HTTPException(status_code=404, detail="token not found")
    return token


class _TokenCreate(BaseModel):
    name: str
    scopes: list[str] = []
    expires_at: datetime | None = None


@router.get("/api/workspaces/{workspace_id}/tokens")
async def list_tokens(
    workspace_id: UUID, repos: Repositories = Depends(get_repositories)
) -> list[dict]:
    tokens = await repos.auth_tokens.list_for_workspace(workspace_id)
    return [_token_payload(t) for t in tokens]


@router.post("/api/workspaces/{workspace_id}/tokens", status_code=201)
async def create_token(
    workspace_id: UUID,
    body: _TokenCreate,
    repos: Repositories = Depends(get_repositories),
) -> dict:
    plaintext, token_hash = issue_api_token()
    validated = AuthToken(
        id=uuid4(),
        workspace_id=workspace_id,
        name=body.name.strip(),
        token_hash=token_hash,
        scopes=list(body.scopes),
        expires_at=body.expires_at,
    )
    token = orm.AuthToken(
        id=validated.id,
        workspace_id=workspace_id,
        name=validated.name,
        token_hash=token_hash,
        scopes=list(validated.scopes),
        expires_at=validated.expires_at,
        created_at=validated.created_at,
    )
    await repos.auth_tokens.add(token)
    payload = _token_payload(token)
    payload["token"] = plaintext
    return payload


@router.get("/api/workspaces/{workspace_id}/tokens/{token_id}")
async def get_token(
    workspace_id: UUID,
    token_id: UUID,
    repos: Repositories = Depends(get_repositories),
) -> dict:
    token = await _token_or_404(repos, workspace_id, token_id)
    return _token_payload(token)


@router.delete("/api/workspaces/{workspace_id}/tokens/{token_id}", status_code=204)
async def revoke_token(
    workspace_id: UUID,
    token_id: UUID,
    repos: Repositories = Depends(get_repositories),
) -> None:
    token = await _token_or_404(repos, workspace_id, token_id)
    await repos.auth_tokens.delete(token)
