"""Login handshake for the Web console (token-based, no passwords).

The console's only credential is an ``auth_tokens`` bearer token — the
§10.2 seed prints one at first boot and the tokens page mints more.
``POST /api/auth/verify`` checks a candidate token and returns the
workspace identity to land on; ``GET /api/auth/me`` re-derives the same
identity from the request's ``Authorization`` header (the global
``require_auth`` dependency has already validated it and FastAPI serves
the cached result to this route).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from orchestratord.api.db import get_repositories
from orchestratord.api.deps import _is_expired, require_auth
from orchestratord.db.repository import Repositories
from orchestratord.domain.auth_token import AuthToken, hash_api_token

router = APIRouter(tags=["auth"])


class _TokenVerify(BaseModel):
    token: str


async def _identity_payload(
    repos: Repositories, record: AuthToken
) -> dict:
    workspace = await repos.workspaces.get(record.workspace_id)
    members = await repos.members.list_for_workspace(record.workspace_id)
    owner = next(
        (m for m in members if m.role == "owner"), members[0] if members else None
    )
    return {
        "workspace_id": str(record.workspace_id),
        "workspace_slug": workspace.slug if workspace is not None else None,
        "member_id": str(owner.id) if owner is not None else None,
        "member_name": owner.name if owner is not None else None,
    }


@router.post("/api/auth/verify")
async def verify_token(
    body: _TokenVerify, repos: Repositories = Depends(get_repositories)
) -> dict:
    plaintext = body.token.strip()
    record = await repos.auth_tokens.by_token_hash(hash_api_token(plaintext))
    if record is None or _is_expired(record.expires_at):
        raise HTTPException(status_code=401, detail="invalid or expired token")
    return await _identity_payload(repos, record)


@router.get("/api/auth/me")
async def me(
    record: AuthToken = Depends(require_auth),
    repos: Repositories = Depends(get_repositories),
) -> dict:
    return await _identity_payload(repos, record)
