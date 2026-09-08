"""Monthly partition management for the ``events`` table (§6.1.2).

``events`` is ``PARTITION BY RANGE (created_at)``, but the parent table has no
children until a month is attached — Postgres rejects rows that match no
partition. :func:`ensure_monthly_partition` attaches the month partition for a
given ``created_at`` before an insert so the :class:`EventRepository` can
append without the caller thinking about partitioning.

A dedicated partition-maintenance job that pre-creates future months is a
follow-up; this module guarantees the current month exists — safely under
concurrency via a transaction-scoped advisory lock keyed on the partition name.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

_PARTITION_PREFIX = "events_"


def _month_bounds(created_at: datetime) -> tuple[datetime, datetime, str]:
    start = created_at.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if start.month == 12:
        end = start.replace(year=start.year + 1, month=1)
    else:
        end = start.replace(month=start.month + 1)
    name = f"{_PARTITION_PREFIX}{start:%Y%m}"
    return start, end, name


async def ensure_monthly_partition(
    session: AsyncSession, created_at: datetime
) -> str:
    """Attach the month partition for *created_at*, returning its name.

    Idempotent and concurrency-safe: concurrent first-writers for a fresh month
    are serialized by a transaction-scoped advisory lock keyed on the partition
    name, so exactly one ``CREATE TABLE`` runs and the losers re-check to find
    the winner's partition. The partition name and bounds are derived from the
    caller-supplied ``created_at`` (a ``datetime``), never from raw SQL text, so
    no user input is interpolated.
    """
    start, end, name = _month_bounds(created_at)

    # The check-then-create below is a TOCTOU window without this: two sessions
    # can both observe the partition absent and race the CREATE, leaving one
    # with a DuplicateTableError. ``pg_advisory_xact_lock`` is released at
    # commit/rollback, so the lock and the partition CREATE share one
    # transaction lifetime.
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtext(:name)::bigint)"),
        {"name": name},
    )

    exists = await session.execute(
        text(
            "SELECT 1 FROM pg_class c "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = 'public' AND c.relname = :name"
        ),
        {"name": name},
    )
    if exists.scalar() is None:
        await session.execute(
            text(
                f"CREATE TABLE {name} PARTITION OF events "
                f"FOR VALUES FROM ('{start.isoformat()}') TO ('{end.isoformat()}')"
            )
        )
    return name
