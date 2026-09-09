"""Authorization principal helpers for the API layer.

Auth runs in one of two modes, selected by ``ORCHESTRATORD_AUTH``:

* **Local single-user (default, unset/``0``)** — the deployment is a
  personal daemon on a trusted host; ``require_auth`` and the WebSocket
  token gate accept everything. The login/multi-user implementation stays
  fully wired (routers, frontend token store, login page) but is inert.
* **Multi-user token mode (``ORCHESTRATORD_AUTH=1``)** — every
  workspace-scoped path requires an ``auth_tokens`` bearer token; the only
  credential is the token itself (no passwords).

The admin gate remains the process-local override below, used by tests and
single-user developer mode for admin-only endpoints such as ``POST
/api/skills/refresh-hashes``.
"""

from __future__ import annotations

import math
import os
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime

from fastapi import Depends, HTTPException, Request

from orchestratord.api.db import get_repositories
from orchestratord.db.models.peer import Peer
from orchestratord.db.repository import Repositories
from orchestratord.domain.auth_token import AuthToken, hash_api_token
from orchestratord.peer.registry import get_peer_by_token_id

_AUTH_ENV = "ORCHESTRATORD_AUTH"


def auth_enabled() -> bool:
    """Return whether multi-user token auth is active for this process.

    Read per call (not cached at import) so tests can flip the switch with
    ``monkeypatch``/``setenv``.
    """
    return os.environ.get(_AUTH_ENV) == "1"

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

    In local single-user mode (``auth_enabled()`` false) every path is
    accepted and ``None`` is returned without touching the database.
    """
    if not auth_enabled():
        return None
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


# ---------------------------------------------------------------------------
# Peer Federation auth (DESIGN §6.2, D25; PR5)
# ---------------------------------------------------------------------------


class TokenBucket:
    """D25 per-peer rate limiter (100 INVOKE/s, burst 200 by default).

    A refill-on-read token bucket keyed by an arbitrary string (the
    peer identity). ``try_acquire`` returns ``None`` when a token was
    consumed, or the ``Retry-After`` seconds when the bucket is empty.
    Guarded by a ``threading.Lock`` — ``TestClient`` dispatches through
    a worker thread (see the admin-override note above), and the
    critical section is a few dict ops.
    """

    def __init__(self, rps: float, burst: int) -> None:
        self._rps = max(rps, 0.001)
        self._burst = float(max(burst, 1))
        self._tokens: dict[str, float] = {}
        self._updated: dict[str, float] = {}
        self._lock = threading.Lock()

    def try_acquire(self, key: str) -> float | None:
        now = time.monotonic()
        with self._lock:
            current = min(
                self._burst,
                self._tokens.get(key, self._burst)
                + (now - self._updated.get(key, now)) * self._rps,
            )
            self._updated[key] = now
            if current >= 1.0:
                self._tokens[key] = current - 1.0
                return None
            self._tokens[key] = current
            return (1.0 - current) / self._rps


_PEER_RATE_BUCKET: TokenBucket | None = None
_PEER_BUCKET_LOCK = threading.Lock()


def _peer_rate_bucket() -> TokenBucket:
    """Lazily build the shared bucket from ``PeerConfig`` (D25 config)."""
    global _PEER_RATE_BUCKET
    with _PEER_BUCKET_LOCK:
        if _PEER_RATE_BUCKET is None:
            from orchestratord.config.schema import PeerConfig

            cfg = PeerConfig.from_env()
            _PEER_RATE_BUCKET = TokenBucket(cfg.rate_limit.rps, cfg.rate_limit.burst)
        return _PEER_RATE_BUCKET


def reset_peer_rate_bucket() -> None:
    """Drop the cached bucket (test seam after env changes)."""
    global _PEER_RATE_BUCKET
    with _PEER_BUCKET_LOCK:
        _PEER_RATE_BUCKET = None


async def require_peer_auth(
    request: Request,
    repos: Repositories = Depends(get_repositories),
) -> Peer:
    """Authenticate a remote peer daemon (D25 + §6.2).

    The caller presents its per-peer bearer token plus
    ``X-Peer-Orchestrator-Id``; the token must hash to a live
    ``auth_tokens`` row with a ``peer.*`` scope, and the
    (orch_id, token) pair must match an ``accepted`` registry row.
    POSTs to the high-throughput endpoints (``/invoke``, ``/sessions``)
    additionally pass the D25 token bucket — over-limit callers get
    ``429`` with ``Retry-After``.
    """
    orch_header = request.headers.get("x-peer-orchestrator-id", "").strip()
    header = request.headers.get("authorization", "")
    if not orch_header or not header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="peer identity or token missing")
    plaintext = header[len("Bearer ") :].strip()
    if not plaintext:
        raise HTTPException(status_code=401, detail="peer identity or token missing")
    token = await repos.auth_tokens.by_token_hash(hash_api_token(plaintext))
    if token is None or _is_expired(token.expires_at):
        raise HTTPException(status_code=401, detail="invalid or expired peer token")
    if not any(scope == "peer.*" or scope.startswith("peer.") for scope in token.scopes):
        raise HTTPException(status_code=403, detail="token lacks peer.* scope")
    peer = await get_peer_by_token_id(repos.session, token.id, orch_header)
    if peer is None or peer.status != "accepted":
        raise HTTPException(status_code=401, detail="peer is not accepted")
    if request.method == "POST" and (
        request.url.path.endswith("/invoke")
        or request.url.path.endswith("/sessions")
    ):
        retry_after = _peer_rate_bucket().try_acquire(str(peer.id))
        if retry_after is not None:
            raise HTTPException(
                status_code=429,
                detail="peer rate limit exceeded",
                headers={"Retry-After": str(math.ceil(retry_after))},
            )
    return peer
