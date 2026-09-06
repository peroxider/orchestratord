"""Per-request async session + repository dependency for the API layer.

This module is the single seam through which the FastAPI routers reach the
persistence layer (``orchestratord.db``).  Routers depend on
:func:`get_repositories`, which yields a :class:`Repositories` facade bound to a
fresh :class:`AsyncSession` and owns the transaction boundary: commit on
successful return, rollback on error.  Routers therefore never touch the raw
session and never call ``commit()`` — they match the repository layer's
commit-free contract (``Repository.add``/``delete`` flush but do not commit).

The session factory is built lazily on first use so importing this module does
not require a live database (tests and offline importers stay cheap), and is
cached for the process lifetime.  ``expire_on_commit=False`` (set in
``build_session_factory``) keeps ORM objects readable after commit, which the
response builders rely on.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from orchestratord.db.engine import build_session_factory
from orchestratord.db.repository import Repositories

_session_factory: async_sessionmaker[AsyncSession] | None = None


def _get_session_factory() -> async_sessionmaker[AsyncSession]:
    global _session_factory
    if _session_factory is None:
        _session_factory = build_session_factory()
    return _session_factory


async def get_repositories() -> AsyncIterator[Repositories]:
    """Yield a repository facade over one transaction-scoped session.

    Commits on clean exit, rolls back on any exception (including a router's
    ``HTTPException``), so a 4xx/5xx never leaves a half-written row behind.
    """
    factory = _get_session_factory()
    async with factory() as session:
        repos = Repositories(session)
        try:
            yield repos
            await session.commit()
        except BaseException:
            await session.rollback()
            raise
