"""Live-Postgres fixtures for the repository/session layer integration tests.

These tests need a running PostgreSQL at ``127.0.0.1:5432`` reachable by the
``multica`` role (user/password/database = ``multica`` — the
``multica-postgres-1`` compose container). The suite creates a *dedicated*
``orchestratord_test`` database and never touches the ``multica`` product
schema (which already holds the reference implementation's tables). When
Postgres is unreachable the fixtures ``pytest.skip`` so the default test run
stays green without a database.

Isolation is per-test: every test gets a fresh engine + session bound to the
*test's* event loop (avoiding asyncpg pool loop-affinity issues), all 30
tables are ``TRUNCATE``-ed up front, and — because there are no foreign keys
(§6.1) — ``Base.metadata.create_all`` is re-run idempotently each test.
"""

from __future__ import annotations

import asyncpg
import pytest
from sqlalchemy import text

from orchestratord.db.base import Base
from orchestratord.db.engine import (
    build_engine,
    build_session_factory,
    create_schema,
)

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


async def _ensure_bucket_index(engine) -> None:
    """Mirror migration 0008's unique usage bucket index.

    ``create_schema`` deliberately builds no indexes (migrations are the
    authoritative source); the usage upsert's ``ON CONFLICT`` needs this
    index to exist. Idempotent via ``IF NOT EXISTS``.
    """
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_usage_aggregates_bucket "
                "ON usage_aggregates (workspace_id, agent_id, issue_id, backend, day) "
                "NULLS NOT DISTINCT"
            )
        )


@pytest.fixture
async def db_engine():
    try:
        await _ensure_test_db()
        engine = build_engine(_TEST_DSN)
        await create_schema(engine)
        await _ensure_bucket_index(engine)
    except Exception as exc:  # noqa: BLE001 — DB down / role missing → skip
        pytest.skip(f"Postgres unavailable at 127.0.0.1:5432: {exc}")
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
async def db(db_engine):
    factory = build_session_factory(db_engine)
    async with factory() as session:
        await _truncate_all(session)
        await session.commit()
        yield session
