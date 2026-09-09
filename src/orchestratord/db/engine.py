"""Async engine + session factory for the §6.1 PostgreSQL schema.

The declarative models in :mod:`orchestratord.db.models` are deliberately free
of driver wiring; this module supplies it. A ``create_async_engine`` +
``async_sessionmaker`` pair (over asyncpg) plus a ``create_schema`` helper that
emits ``Base.metadata.create_all`` for tests and local bring-up. Production
applies the Alembic migrations instead (indexes live there, not in the models).

The DSN is read from ``ORCHESTRATORD_DATABASE_URL`` with a local-dev default so
the daemon and the integration tests share one configuration surface.
"""

from __future__ import annotations

import os
from typing import Final

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from orchestratord.db.base import Base

_ENV_NAME: Final = "ORCHESTRATORD_DATABASE_URL"

# Short asyncpg connect timeout: the default is 60s, which turns a
# black-holed 5432 (SYN dropped, no RST) into a 60s hang per DB touch.
# 3s keeps the daemon responsive and tests fast when PG is unreachable.
_DEFAULT_CONNECT_TIMEOUT_S: Final = 3

DEFAULT_DATABASE_URL: Final = (
    "postgresql+asyncpg://multica:multica@127.0.0.1:5432/multica"
)
"""Local-dev default DSN, matching the ``multica-postgres-1`` container."""


def database_url() -> str:
    """Return the active DSN, preferring ``ORCHESTRATORD_DATABASE_URL``."""
    return os.environ.get(_ENV_NAME) or DEFAULT_DATABASE_URL


def build_engine(url: str | None = None) -> AsyncEngine:
    """Construct an async engine for *url* (or the configured default)."""
    return create_async_engine(
        url or database_url(),
        connect_args={"timeout": _DEFAULT_CONNECT_TIMEOUT_S},
    )


def build_session_factory(
    engine: AsyncEngine | None = None,
) -> async_sessionmaker[AsyncSession]:
    """Return an ``async_sessionmaker`` bound to *engine* (or a fresh one)."""
    return async_sessionmaker(engine or build_engine(), expire_on_commit=False)


async def create_schema(engine: AsyncEngine) -> None:
    """Create every §6.1 table (no indexes — those are migrations) on *engine*.

    Indexes are deliberately absent: the ``CREATE INDEX CONCURRENTLY``
    migrations (``alembic/versions/*``) are the authoritative source for them.
    """
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
