"""Authorization principal helpers for the API layer.

The Phase 0 API has no full authentication layer yet — that lands with the
cookie-session + API-token work in Phase 2 (``docs/FEATURE_GAP_VS_MULTICA.md``
§5.7.4).  Until then the only admin gate is the process-local override below,
which tests and the single-user developer mode use to reach admin-only
endpoints such as ``POST /api/skills/refresh-hashes``.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from fastapi import HTTPException

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
