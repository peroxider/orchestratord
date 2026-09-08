"""``events`` RANGE-partition auto-attach tests (§6.1.2, live DB).

``events`` is ``PARTITION BY RANGE (created_at)`` with no child partitions
until a month is attached — Postgres rejects any row matching no partition.
:meth:`EventRepository.append` therefore calls
:func:`orchestratord.db.partitions.ensure_monthly_partition` before insert.
These tests prove the partition is created transparently, that two events in
different months land in distinct partitions, and that the lookup orderings
(session sequence ASC, workspace/issue created_at DESC) hold.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import text

from orchestratord.db.engine import build_session_factory
from orchestratord.db.models import Event
from orchestratord.db.repository import Repositories

pytestmark = pytest.mark.database


def _event(**overrides) -> Event:
    defaults = {
        "id": uuid.uuid4(),
        "session_id": uuid.uuid4(),
        "sequence": 1,
        "kind": "message",
        "payload": {"text": "hi"},
        "run_id": None,
        "issue_id": None,
        "workspace_id": uuid.uuid4(),
        "created_at": datetime.now(UTC),
    }
    defaults.update(overrides)
    return Event(**defaults)


async def test_append_creates_partition_and_round_trips(db) -> None:
    repos = Repositories(db)
    ev = _event()
    await repos.events.append(ev)
    await db.commit()
    name = f"events_{ev.created_at:%Y%m}"
    row = await db.execute(
        text("SELECT 1 FROM pg_class WHERE relname = :name"), {"name": name}
    )
    assert row.scalar() == 1
    assert [e.id for e in await repos.events.list_for_session(ev.session_id)] == [
        ev.id
    ]


async def test_cross_month_events_in_distinct_partitions(db) -> None:
    repos = Repositories(db)
    base = datetime.now(UTC).replace(day=1)
    if base.month == 1:
        prev = base.replace(year=base.year - 1, month=12)
    else:
        prev = base.replace(month=base.month - 1)
    e1 = _event(created_at=prev)
    e2 = _event(created_at=base)
    await repos.events.append(e1)
    await repos.events.append(e2)
    await db.commit()
    p1 = f"events_{prev:%Y%m}"
    p2 = f"events_{base:%Y%m}"
    assert p1 != p2
    rows = await db.execute(
        text("SELECT relname FROM pg_class WHERE relname IN (:p1, :p2)"),
        {"p1": p1, "p2": p2},
    )
    assert {r[0] for r in rows} == {p1, p2}


async def test_list_for_session_respects_sequence_bounds(db) -> None:
    repos = Repositories(db)
    sid = uuid.uuid4()
    for seq in (1, 2, 3, 4):
        await repos.events.append(_event(session_id=sid, sequence=seq))
    assert [e.sequence for e in await repos.events.list_for_session(sid)] == [
        1,
        2,
        3,
        4,
    ]
    mid = await repos.events.list_for_session(sid, from_seq=2, to_seq=3)
    assert [e.sequence for e in mid] == [2, 3]


async def test_list_for_workspace_and_issue(db) -> None:
    repos = Repositories(db)
    ws = uuid.uuid4()
    issue = uuid.uuid4()
    await repos.events.append(_event(workspace_id=ws, issue_id=issue, sequence=1))
    await repos.events.append(_event(workspace_id=ws, issue_id=None, sequence=2))
    await repos.events.append(
        _event(workspace_id=uuid.uuid4(), issue_id=issue, sequence=3)
    )
    assert len(await repos.events.list_for_workspace(ws)) == 2
    assert len(await repos.events.list_for_issue(issue)) == 2


async def test_concurrent_append_to_fresh_month_is_safe(db_engine) -> None:
    """Concurrent first-writers to a new month must not race the CREATE.

    Regression test for the TOCTOU window in ``ensure_monthly_partition``: ten
    sessions append events for the same not-yet-partitioned month at once. The
    advisory lock must serialize them so exactly one partition is created and
    all ten rows land in it (previously 9/10 failed with DuplicateTableError).
    """
    month = datetime(2099, 1, 15, tzinfo=UTC)
    name = "events_209901"
    factory = build_session_factory(db_engine)

    async with factory() as setup:
        await setup.execute(text(f"DROP TABLE IF EXISTS {name}"))
        await setup.commit()

    async def append_one(i: int) -> uuid.UUID:
        async with factory() as session:
            ev = _event(created_at=month, sequence=i, payload={"i": i})
            await Repositories(session).events.append(ev)
            await session.commit()
            return ev.id

    ids = await asyncio.gather(*(append_one(i) for i in range(10)))
    assert len(set(ids)) == 10

    async with factory() as check:
        count = await check.execute(text(f"SELECT count(*) FROM {name}"))
        assert count.scalar() == 10
