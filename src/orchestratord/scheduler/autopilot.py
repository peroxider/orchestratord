"""AutopilotScheduler: croniter-driven workflow triggering (§7.1).

Asyncio loop (no APScheduler) polling ``enabled`` autopilots on a fixed
interval. For each autopilot the latest cron slot at-or-before ``now`` is
computed with :class:`croniter.croniter`; a run row is inserted for that
slot (dedup keyed on ``(autopilot_id, scheduled_at)``, so restarts and
overlapping ticks never double-fire) and the ``invoke`` seam is awaited.

The seam mirrors the chat dispatcher's ``runner_invoke``: the default
implementation creates the autopilot's issue (§7.5 acceptance: trigger
produces issue + run row); a real workflow launch can replace it without
touching the cron mechanics. After a successful invoke, rows still in
``queued`` are flipped to ``completed`` — an async workflow invocation that
manages its own status is never overwritten (same no-resurrect rule as
:mod:`orchestratord.chat_dispatcher`). Missed slots while the daemon was
down are not replayed: only the newest slot fires.

Lifespan: ``serve`` starts the scheduler alongside the chat daemon behind
``ORCHESTRATORD_AUTOPILOT_DAEMON=1``; ``stop()`` flips the flag, grants a
short grace period, then cancels.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import UTC, datetime
from typing import Any, Awaitable, Callable

from croniter import croniter
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from orchestratord.db import models as orm
from orchestratord.db.repository import Repositories

logger = logging.getLogger(__name__)

AutopilotInvoke = Callable[[orm.Autopilot, orm.AutopilotRun], Awaitable[None]]


def last_due_slot(cron: str, now: datetime) -> datetime:
    """Most recent cron fire time at-or-before *now* (UTC)."""
    return croniter(cron, now).get_prev(datetime)


async def default_invoke(
    session_factory: Any, autopilot: orm.Autopilot, run: orm.AutopilotRun
) -> None:
    """Phase-1 trigger body: create the autopilot's issue (§7.5)."""
    async with session_factory() as db:
        repos = Repositories(db)
        await repos.issues.add(
            orm.Issue(
                id=uuid.uuid4(),
                workspace_id=autopilot.workspace_id,
                title=f"[autopilot] {autopilot.name}",
                description=autopilot.prompt,
                status="pending",
                assignee_type=None,
                assignee_id=None,
                created_at=datetime.now(UTC),
            )
        )
        await db.commit()


def build_default_invoke(session_factory: Any) -> AutopilotInvoke:
    async def invoke(autopilot: orm.Autopilot, run: orm.AutopilotRun) -> None:
        await default_invoke(session_factory, autopilot, run)

    return invoke


class AutopilotScheduler:
    """Cron loop over enabled autopilots (§7.1)."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        invoke: AutopilotInvoke | None = None,
        interval: float = 60.0,
    ) -> None:
        self._factory = session_factory
        self._invoke = invoke
        self._interval = interval
        self._running = False
        self._loop_task: asyncio.Task[Any] | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        if self._loop_task is not None and not self._loop_task.done():
            return
        self._running = True
        self._loop_task = asyncio.create_task(
            self._loop(), name="autopilot-scheduler"
        )

    async def stop(self) -> None:
        self._running = False
        task = self._loop_task
        if task is None or task.done():
            return
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=5.0)
            return
        except asyncio.TimeoutError:
            task.cancel()
        except Exception:
            logger.debug("autopilot loop ended with error", exc_info=True)
            return
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def _loop(self) -> None:
        while self._running:
            try:
                await self.tick()
            except Exception:
                logger.exception("autopilot tick failed")
            await asyncio.sleep(self._interval)

    # ------------------------------------------------------------------
    # Trigger pass
    # ------------------------------------------------------------------

    async def tick(self, now: datetime | None = None) -> int:
        """Fire every due autopilot once; return how many fired."""
        current = now or datetime.now(UTC)
        autopilots = await self._list_enabled()
        fired = 0
        for autopilot in autopilots:
            try:
                due = self._is_due(autopilot, current)
            except Exception:
                logger.exception(
                    "autopilot %s has an invalid cron %r", autopilot.id, autopilot.cron
                )
                continue
            if not due:
                continue
            run = await self._claim_slot(autopilot, current)
            if run is None:
                continue  # slot already fired
            fired += 1
            try:
                invoke = self._invoke or build_default_invoke(self._factory)
                await invoke(autopilot, run)
            except Exception:
                logger.exception(
                    "autopilot %s invoke failed", autopilot.id, exc_info=True
                )
                await self._mark_failed(run.id)
                continue
            await self._complete_if_queued(run.id)
        return fired

    async def _list_enabled(self) -> list[orm.Autopilot]:
        async with self._factory() as db:
            rows = await db.execute(
                select(orm.Autopilot).where(orm.Autopilot.enabled.is_(True))
            )
            return list(rows.scalars().all())

    def _is_due(self, autopilot: orm.Autopilot, now: datetime) -> bool:
        """True when the latest cron slot is fresh within one interval.

        A slot older than one interval means the daemon was down — one
        fire covers the gap, older slots are skipped (no replay).
        """
        slot = last_due_slot(autopilot.cron, now)
        return (now - slot).total_seconds() < self._interval

    async def _claim_slot(
        self, autopilot: orm.Autopilot, now: datetime
    ) -> orm.AutopilotRun | None:
        """Insert the run row for this cron slot, or ``None`` if taken."""
        slot = last_due_slot(autopilot.cron, now)
        async with self._factory() as db:
            repos = Repositories(db)
            existing = await repos.autopilot_runs.find_for_slot(autopilot.id, slot)
            if existing is not None:
                return None
            run = orm.AutopilotRun(
                id=uuid.uuid4(),
                autopilot_id=autopilot.id,
                scheduled_at=slot,
                run_id=uuid.uuid4(),
                status="queued",
                started_at=now,
                finished_at=None,
            )
            await repos.autopilot_runs.add(run)
            await db.commit()
            return run

    async def _complete_if_queued(self, run_id: uuid.UUID) -> None:
        async with self._factory() as db:
            row = (
                await db.execute(
                    select(orm.AutopilotRun)
                    .where(orm.AutopilotRun.id == run_id)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if row is not None and row.status == "queued":
                row.status = "completed"
                row.finished_at = datetime.now(UTC)
                await db.commit()

    async def _mark_failed(self, run_id: uuid.UUID) -> None:
        async with self._factory() as db:
            row = (
                await db.execute(
                    select(orm.AutopilotRun)
                    .where(orm.AutopilotRun.id == run_id)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if row is not None:
                row.status = "failed"
                row.finished_at = datetime.now(UTC)
                await db.commit()


__all__ = [
    "AutopilotInvoke",
    "AutopilotScheduler",
    "build_default_invoke",
    "default_invoke",
    "last_due_slot",
]
