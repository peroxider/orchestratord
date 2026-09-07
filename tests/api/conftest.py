"""Live-Postgres fixtures for the rewired CRUD-router contract tests.

The ten dict-backed routers (projects, runtimes, channels, audit, tokens,
inbox, issues, members, autopilots, squads) now read/write the SQLAlchemy
repository layer behind :func:`orchestratord.api.db.get_repositories`. Their
tests therefore hit the real FastAPI app over httpx's ASGI transport, with the
repository dependency overridden to a function-scoped async session bound to
the dedicated ``orchestratord_test`` database.

Isolation is per-test: a fresh app is built (so ``dependency_overrides`` never
leaks across tests), all 30 tables are ``TRUNCATE``-ed once up front, and each
request runs in its own session that commits on success / rolls back on error.
When Postgres is unreachable the ``client`` fixture skips, so the default test
run stays green without a database (mirroring ``tests/db_integration``).
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import asyncpg
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from orchestratord.api.app import create_app
from orchestratord.api.db import get_repositories
from orchestratord.db.base import Base
from orchestratord.db.engine import (
    build_engine,
    build_session_factory,
    create_schema,
)
from orchestratord.db.repository import Repositories

_ADMIN_DSN = "postgresql://multica:multica@127.0.0.1:5432/multica"
_TEST_DB = "orchestratord_test"
_TEST_DSN = f"postgresql+asyncpg://multica:multica@127.0.0.1:5432/{_TEST_DB}"


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "database: requires a live PostgreSQL (orchestratord_test); "
        "skipped when unreachable",
    )


async def _ensure_test_db() -> None:
    admin = await asyncpg.connect(_ADMIN_DSN)
    try:
        exists = await admin.fetchval(
            "SELECT 1 FROM pg_database WHERE datname = $1", _TEST_DB
        )
        if not exists:
            await admin.execute(f"CREATE DATABASE {_TEST_DB}")
    finally:
        await admin.close()


async def _truncate_all(session) -> None:
    names = ", ".join(t.name for t in Base.metadata.sorted_tables)
    await session.execute(text(f"TRUNCATE TABLE {names}"))


@pytest.fixture
async def db_engine():
    try:
        await _ensure_test_db()
        engine = build_engine(_TEST_DSN)
        await create_schema(engine)
    except Exception as exc:  # noqa: BLE001 — DB down / role missing → skip
        pytest.skip(f"Postgres unavailable at 127.0.0.1:5432: {exc}")
    try:
        yield engine
    finally:
        await engine.dispose()


def _repo_override(factory):
    """Build a ``get_repositories`` override bound to *factory*.

    Shared by the HTTP ``client`` fixture and the WebSocket tests (which
    need the same test-DB binding on a fresh ``create_app()``).
    """

    async def override_get_repositories() -> AsyncIterator[Repositories]:
        async with factory() as session:
            repos = Repositories(session)
            try:
                yield repos
                await session.commit()
            except BaseException:
                await session.rollback()
                raise

    return override_get_repositories


@pytest.fixture
async def client(db_engine):
    factory = build_session_factory(db_engine)

    async with factory() as session:
        await _truncate_all(session)
        await session.commit()

    app = create_app()
    app.dependency_overrides[get_repositories] = _repo_override(factory)
    # The shared contract tests exercise the routers unauthenticated; the
    # global token gate is lifted here and exercised for real in
    # tests/api/test_auth.py (fresh app, no override).
    from orchestratord.api.deps import require_auth

    app.dependency_overrides[require_auth] = lambda: None
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest.fixture
async def db(db_engine, client):
    """A session for seeding rows the API has no create endpoint for.

    Sessions and their event timeline are inserted by the backend runner, not
    the Web client, so the sessions contract tests seed ``Session`` / ``Event``
    rows directly and then drive the read/lifecycle endpoints over HTTP.
    Depends on ``client`` so the per-test ``TRUNCATE`` runs before seeding.
    """
    factory = build_session_factory(db_engine)
    async with factory() as session:
        yield session
