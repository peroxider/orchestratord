"""AutopilotScheduler behavior tests (§7.1) — live ``orchestratord_test`` DB.

Pins the tick semantics: due-slot firing with run-row + issue creation,
same-slot dedup across ticks, disabled/invalid-cron skipping, stale-slot
(non-replay) behavior, and the invoke-failure → ``failed`` path. Skips
when Postgres is unreachable.

Reference: docs/FEATURE_GAP_VS_MULTICA.md §7.1, §7.5.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import select

from orchestratord.db import models as orm
from orchestratord.db.engine import build_session_factory
from orchestratord.scheduler.autopilot import AutopilotScheduler

pytestmark = pytest.mark.database

_NOW = datetime(2026, 9, 7, 10, 7, 30, tzinfo=UTC)  # slot 10:07, fresh


async def _seed_autopilot(db, *, cron: str = "* * * * *", enabled: bool = True) -> orm.Autopilot:
    ws = orm.Workspace(
        id=uuid4(), slug=f"ws-{uuid4().hex[:8]}", name="WS",
        created_at=datetime.now(UTC),
    )
    autopilot = orm.Autopilot(
        id=uuid4(),
        workspace_id=ws.id,
        name="nightly regression",
        cron=cron,
        prompt="run the regression suite",
        target_kind="issue",
        target_id=uuid4(),
        enabled=enabled,
    )
    db.add_all([ws, autopilot])
    await db.commit()
    return autopilot


def _scheduler(db_engine, invoke=None, interval: float = 60.0) -> AutopilotScheduler:
    return AutopilotScheduler(
        build_session_factory(db_engine), invoke=invoke, interval=interval
    )


class TestTick:
    async def test_fires_due_autopilot_and_creates_run_and_issue(self, db, db_engine) -> None:
        autopilot = await _seed_autopilot(db)
        scheduler = _scheduler(db_engine)  # default invoke creates the issue

        fired = await scheduler.tick(now=_NOW)

        assert fired == 1
        runs = (
            await db.execute(
                select(orm.AutopilotRun).where(
                    orm.AutopilotRun.autopilot_id == autopilot.id
                )
            )
        ).scalars().all()
        assert len(runs) == 1
        assert runs[0].status == "completed"
        assert runs[0].scheduled_at == datetime(2026, 9, 7, 10, 7, 0, tzinfo=UTC)
        assert runs[0].finished_at is not None
        issues = (
            await db.execute(
                select(orm.Issue).where(orm.Issue.workspace_id == autopilot.workspace_id)
            )
        ).scalars().all()
        assert [i.title for i in issues] == ["[autopilot] nightly regression"]
        assert issues[0].description == "run the regression suite"

    async def test_same_slot_not_fired_twice(self, db, db_engine) -> None:
        await _seed_autopilot(db)
        scheduler = _scheduler(db_engine)

        first = await scheduler.tick(now=_NOW)
        second = await scheduler.tick(
            now=datetime(2026, 9, 7, 10, 7, 50, tzinfo=UTC)
        )

        assert (first, second) == (1, 0)
        runs = (await db.execute(select(orm.AutopilotRun))).scalars().all()
        assert len(runs) == 1

    async def test_disabled_autopilot_skipped(self, db, db_engine) -> None:
        await _seed_autopilot(db, enabled=False)
        scheduler = _scheduler(db_engine)
        assert await scheduler.tick(now=_NOW) == 0

    async def test_invalid_cron_skipped_without_crash(self, db, db_engine) -> None:
        await _seed_autopilot(db, cron="not a cron")
        scheduler = _scheduler(db_engine)
        assert await scheduler.tick(now=_NOW) == 0
        runs = (await db.execute(select(orm.AutopilotRun))).scalars().all()
        assert runs == []

    async def test_stale_slot_not_replayed(self, db, db_engine) -> None:
        """Slot older than one interval (daemon was down) must not fire."""
        await _seed_autopilot(db, cron="0 2 * * *")  # slot 02:00, ~8h stale
        scheduler = _scheduler(db_engine, interval=60.0)
        assert await scheduler.tick(now=_NOW) == 0

    async def test_invoke_failure_marks_run_failed(self, db, db_engine) -> None:
        autopilot = await _seed_autopilot(db)

        async def boom(autopilot, run):
            raise RuntimeError("workflow launch failed")

        scheduler = _scheduler(db_engine, invoke=boom)
        assert await scheduler.tick(now=_NOW) == 1  # fired, then failed
        runs = (
            await db.execute(
                select(orm.AutopilotRun).where(
                    orm.AutopilotRun.autopilot_id == autopilot.id
                )
            )
        ).scalars().all()
        assert len(runs) == 1
        assert runs[0].status == "failed"
        assert runs[0].finished_at is not None

    async def test_custom_invoke_receives_queued_run(self, db, db_engine) -> None:
        autopilot = await _seed_autopilot(db)
        seen: list[tuple[str, str]] = []

        async def invoke(ap, run):
            seen.append((ap.id, run.status))

        scheduler = _scheduler(db_engine, invoke=invoke)
        await scheduler.tick(now=_NOW)
        assert seen == [(autopilot.id, "queued")]


class TestLifecycle:
    async def test_start_stop_loop(self, db_engine) -> None:
        scheduler = _scheduler(db_engine, interval=0.01)
        await scheduler.start()
        task = scheduler._loop_task
        assert task is not None and not task.done()
        await scheduler.stop()
        assert task.done()

    async def test_start_is_idempotent(self, db_engine) -> None:
        scheduler = _scheduler(db_engine, interval=0.01)
        await scheduler.start()
        first = scheduler._loop_task
        await scheduler.start()
        assert scheduler._loop_task is first
        await scheduler.stop()
