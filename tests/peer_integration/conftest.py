"""Fixtures for the cross-process peer federation integration tests.

PR-B9 (D5): the frame-transport canary needs a real JSONB schema —
SQLite cannot compile it, which left the true-socket regression
permanently skipped and hid the PR-B7 body-after-response breakage
(21 buffered TestClient tests stayed green while the real wire hung).
The ``peer_pg`` fixture replicates the ``tests/db_integration``
pattern: a dedicated ``orchestratord_test`` database at
``127.0.0.1:5432`` (``multica`` role), skipped when PostgreSQL is
unreachable.
"""

from __future__ import annotations

import asyncpg
import pytest
from sqlalchemy import text

from orchestratord.db.base import Base
from orchestratord.db.engine import build_engine, create_schema

_ADMIN_DSN = "postgresql://multica:multica@127.0.0.1:5432/multica"
_TEST_DB = "orchestratord_test"
_TEST_DSN = f"postgresql+asyncpg://multica:multica@127.0.0.1:5432/{_TEST_DB}"


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "database: requires a live PostgreSQL (scratch DBs); "
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


@pytest.fixture
async def peer_pg():
    """Yield the asyncpg DSN of the shared, truncated test database."""
    try:
        await _ensure_test_db()
        engine = build_engine(_TEST_DSN)
        await create_schema(engine)
        async with engine.begin() as conn:
            names = ", ".join(t.name for t in Base.metadata.sorted_tables)
            await conn.execute(text(f"TRUNCATE TABLE {names}"))
    except Exception as exc:  # noqa: BLE001 — DB down / role missing → skip
        pytest.skip(f"Postgres unavailable at 127.0.0.1:5432: {exc}")
    try:
        yield _TEST_DSN
    finally:
        await engine.dispose()
