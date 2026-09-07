"""Auth contract: the global ``require_auth`` gate + login handshake.

The shared ``client`` fixture lifts the gate (``require_auth`` override)
for the unauthenticated CRUD contract tests; here the gate itself is the
SUT, so a fresh ``create_app()`` is built with only the repository
override — every protected path must 401 without a valid bearer token.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from orchestratord.api.app import create_app
from orchestratord.api.db import get_repositories
from orchestratord.db import models as orm
from orchestratord.db.engine import build_session_factory
from orchestratord.domain.auth_token import issue_api_token
from tests.api.conftest import _repo_override

pytestmark = pytest.mark.database


@pytest.fixture
async def auth_env(db_engine, client):
    """Yield ``(httpx client, token plaintext, workspace_id)`` — gate live.

    Depends on ``client`` for the per-test ``TRUNCATE`` ordering, then
    seeds one workspace + owner member + never-expiring token and binds a
    fresh app (repository override only — ``require_auth`` runs for real).
    """
    factory = build_session_factory(db_engine)
    plaintext, token_hash = issue_api_token()
    ws_id = uuid4()
    now = datetime.now(UTC)
    async with factory() as session:
        session.add(
            orm.Workspace(id=ws_id, slug="auth-ws", name="Auth WS", created_at=now)
        )
        session.add(
            orm.Member(
                id=uuid4(),
                workspace_id=ws_id,
                role="owner",
                name="Owner",
                created_at=now,
            )
        )
        session.add(
            orm.AuthToken(
                id=uuid4(),
                workspace_id=ws_id,
                name="auth-test",
                token_hash=token_hash,
                scopes=[],
                expires_at=None,
                created_at=now,
            )
        )
        await session.commit()

    app = create_app()
    app.dependency_overrides[get_repositories] = _repo_override(factory)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac, plaintext, ws_id


async def test_verify_accepts_valid_token(auth_env) -> None:
    ac, plaintext, ws_id = auth_env
    resp = await ac.post("/api/auth/verify", json={"token": plaintext})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["workspace_id"] == str(ws_id)
    assert body["workspace_slug"] == "auth-ws"
    assert body["member_name"] == "Owner"


async def test_verify_rejects_unknown_token(auth_env) -> None:
    ac, _plaintext, _ws = auth_env
    resp = await ac.post("/api/auth/verify", json={"token": "no-such-token"})
    assert resp.status_code == 401


async def test_protected_route_401_without_header(auth_env) -> None:
    ac, _plaintext, ws_id = auth_env
    resp = await ac.get(f"/api/workspaces/{ws_id}/tokens")
    assert resp.status_code == 401


async def test_protected_route_200_with_valid_bearer(auth_env) -> None:
    ac, plaintext, ws_id = auth_env
    resp = await ac.get(
        f"/api/workspaces/{ws_id}/tokens",
        headers={"Authorization": f"Bearer {plaintext}"},
    )
    assert resp.status_code == 200, resp.text
    tokens = resp.json()
    assert [t["name"] for t in tokens] == ["auth-test"]


async def test_health_stays_public(auth_env) -> None:
    ac, _plaintext, _ws = auth_env
    resp = await ac.get("/api/health")
    assert resp.status_code == 200


async def test_me_returns_identity_with_bearer(auth_env) -> None:
    ac, plaintext, ws_id = auth_env
    resp = await ac.get(
        "/api/auth/me", headers={"Authorization": f"Bearer {plaintext}"}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["workspace_slug"] == "auth-ws"


async def test_token_creation_requires_valid_bearer(auth_env) -> None:
    """A bad bearer cannot mint tokens — the whole surface is gated."""
    ac, _plaintext, ws_id = auth_env
    resp = await ac.post(
        f"/api/workspaces/{ws_id}/tokens",
        headers={"Authorization": "Bearer whatever"},
        json={"name": "expired-probe"},
    )
    assert resp.status_code == 401


async def test_expired_token_rejected(auth_env, db_engine) -> None:
    """An ``auth_tokens`` row past ``expires_at`` must fail every gate path.

    Exercises the ``_is_expired`` branch of ``require_auth`` / the verify
    endpoint — the live counterpart of the never-expiring seed token the
    other tests use.
    """
    from datetime import timedelta

    ac, _plaintext, ws_id = auth_env
    factory = build_session_factory(db_engine)
    plaintext, token_hash = issue_api_token()
    now = datetime.now(UTC)
    async with factory() as session:
        session.add(
            orm.AuthToken(
                id=uuid4(),
                workspace_id=ws_id,
                name="expired-probe",
                token_hash=token_hash,
                scopes=[],
                expires_at=now - timedelta(hours=1),
                created_at=now,
            )
        )
        await session.commit()

    assert (
        await ac.post("/api/auth/verify", json={"token": plaintext})
    ).status_code == 401
    resp = await ac.get(
        f"/api/workspaces/{ws_id}/tokens",
        headers={"Authorization": f"Bearer {plaintext}"},
    )
    assert resp.status_code == 401
    assert "expired" in resp.json()["detail"]
