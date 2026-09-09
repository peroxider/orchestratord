"""Live-Postgres fixtures for scheduler tests.

Mirrors the essential bits of ``tests/api/conftest.py`` (engine creation,
schema sync, per-test TRUNCATE) without importing it, so scheduler tests
stay isolated from the API contract suite. Skips when Postgres is
unreachable, matching the rest of the suite.
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
    admin = await asyncpg.connect(_ADMIN_DSN, timeout=3)
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


@pytest.fixture
async def db(db_engine):
    """Fresh truncated session per test."""
    factory = build_session_factory(db_engine)
    async with factory() as session:
        await _truncate_all(session)
        await session.commit()
        yield session
