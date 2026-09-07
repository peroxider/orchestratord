"""Authorization principal helpers for the API layer.

The global ``require_auth`` dependency puts every workspace-scoped path
behind an ``auth_tokens`` bearer token; the only credential is the token
itself (no passwords).  The admin gate remains the process-local override
below, used by tests and single-user developer mode for admin-only
endpoints such as ``POST /api/skills/refresh-hashes``.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime

from fastapi import Depends, HTTPException, Request

from orchestratord.api.db import get_repositories
from orchestratord.db.repository import Repositories
from orchestratord.domain.auth_token import AuthToken, hash_api_token

# Paths reachable without a bearer token: the login handshake itself,
# liveness, the WebSocket (which authenticates via its own ``token`` query
# parameter), and the OpenAPI docs.
PUBLIC_PATHS = frozenset(
    {
        "/api/health",
        "/ws",
        "/openapi.json",
        "/docs",
        "/redoc",
        "/docs/oauth2-redirect",
    }
)


def _is_expired(expires_at: datetime | None) -> bool:
    """Expiry check for an ORM ``auth_tokens`` row (no dataclass helper)."""
    if expires_at is None:
        return False
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    return datetime.now(UTC) >= expires_at


async def require_auth(
    request: Request,
    repos: Repositories = Depends(get_repositories),
) -> AuthToken | None:
    """FastAPI dependency: reject any non-public path without a valid token.

    The bearer token's SHA-256 must match a persisted ``auth_tokens`` row
    that has not expired.  Returns the matched token so routes that need
    the principal (e.g. ``GET /api/auth/me``) can inject it via the same
    dependency (FastAPI caches the result per request).
    """
    path = request.url.path
    if path in PUBLIC_PATHS or path == "/api/auth/verify":
        return None
    header = request.headers.get("authorization", "")
    if not header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    plaintext = header[len("Bearer ") :].strip()
    if not plaintext:
        raise HTTPException(status_code=401, detail="missing bearer token")
    record = await repos.auth_tokens.by_token_hash(hash_api_token(plaintext))
    if record is None or _is_expired(record.expires_at):
        raise HTTPException(status_code=401, detail="invalid or expired token")
    return record


# Process-local admin override.  Deliberately a plain module global rather
# than a ContextVar: ``fastapi.testclient.TestClient`` dispatches requests
# through a worker thread, so a ContextVar set on the test thread would not
# be visible inside the request.  A module global is shared across threads
# and is set/cleared synchronously around the ``client.post(...)`` call.
_admin_override = False


@contextmanager
def admin_principal_override() -> Iterator[None]:
    """Grant admin access for the duration of the context (test/dev backdoor)."""
    global _admin_override
    previous = _admin_override
    _admin_override = True
    try:
        yield
    finally:
        _admin_override = previous


def principal_is_admin() -> bool:
    """Return whether the current request principal has admin rights."""
    return _admin_override


def require_admin() -> None:
    """FastAPI dependency that 403s unless the principal is an admin."""
    if not principal_is_admin():
        raise HTTPException(status_code=403, detail="admin role required")
