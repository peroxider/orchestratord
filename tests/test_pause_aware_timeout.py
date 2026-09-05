"""Outer orchestration watchdog must not cancel operator-paused work."""

import asyncio
from types import SimpleNamespace

import pytest


@pytest.mark.asyncio
async def test_outer_timeout_excludes_pause_and_still_enforces_active_budget():
    from orchestratord.runner_utils import _await_with_active_timeout

    session = SimpleNamespace(paused=False, timeout_deadline_at=None)
    cancelled = asyncio.Event()

    async def run():
        try:
            await asyncio.sleep(10)
        finally:
            cancelled.set()

    task = asyncio.create_task(_await_with_active_timeout(run(), session, .15))
    await asyncio.sleep(.04)
    session.paused = True
    await asyncio.sleep(.25)
    assert not task.done()
    assert session.timeout_deadline_at is None
    session.paused = False
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(task, .4)
    assert cancelled.is_set()
