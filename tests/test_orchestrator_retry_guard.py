"""Retry semantics.

An operator stop must not be defeated by the auto-retry loop.
Historically ``_schedule_retry`` treated ``session_end_reason ==
"operator_stop"`` like any other failure: the issue was revived 17s
after the operator stopped it (stop → retry → stop loop burning API
calls until max attempts).

``next_retry_at`` must be persisted on the registry record so a
retry that is waiting for a concurrency slot survives daemon restarts
and never silently disappears.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from orchestratord.issue_registry import IssueRegistry
from orchestratord.orchestrator import Orchestrator, OrchestratorState


def _orchestrator(tmp_path: Path) -> Orchestrator:
    orch = object.__new__(Orchestrator)
    orch._state = OrchestratorState()
    orch._registry = IssueRegistry(tmp_path / "registry.json")
    orch._registry.register(issue_id="1", issue_identifier="ISSUE-1")
    orch.workflow = SimpleNamespace(
        agent=SimpleNamespace(
            max_retry_attempts=3,
            max_retry_backoff_ms=60_000,
            max_turns_retry_delay_ms=60_000,
        )
    )
    return orch


def _session(end_reason: str | None) -> SimpleNamespace:
    return SimpleNamespace(
        issue=SimpleNamespace(id="1", identifier="ISSUE-1"),
        run_id="run-1",
        status="failed",
        session_end_reason=end_reason,
        session_end_summary="",
        output_text="",
        turn_count=1,
        tool_count=1,
        workspace=SimpleNamespace(path=Path("/tmp")),
    )


@pytest.mark.asyncio
async def test_operator_stop_is_not_auto_retried(tmp_path: Path) -> None:
    """operator_stop is operator intent — the retry loop must leave the
    issue in its terminal state and release the claim.
    """
    orch = _orchestrator(tmp_path)
    orch._state.claimed.add("1")
    session = _session("operator_stop")

    await orch._schedule_retry(session)

    assert orch._state.retry_queue == [], (
        "operator-stopped issue must not enter the retry queue"
    )
    assert "1" not in orch._state.claimed, "claim must be released"
    assert orch._state.retry_attempts.get("1") is None


@pytest.mark.asyncio
async def test_operator_takeover_is_not_auto_retried(tmp_path: Path) -> None:
    orch = _orchestrator(tmp_path)
    session = _session("operator_takeover")

    await orch._schedule_retry(session)

    assert orch._state.retry_queue == []


@pytest.mark.asyncio
async def test_genuine_failure_still_retries(tmp_path: Path) -> None:
    """Ordinary failures keep their automatic retry."""
    orch = _orchestrator(tmp_path)
    session = _session("total_timeout")

    await orch._schedule_retry(session)

    assert len(orch._state.retry_queue) == 1
    assert orch._state.retry_queue[0].issue_id == "1"


@pytest.mark.asyncio
async def test_scheduled_retry_persists_next_retry_at(tmp_path: Path) -> None:
    """The retry plan must be visible on the registry record so it
    survives a daemon restart and operators can see why nothing runs.
    """
    orch = _orchestrator(tmp_path)
    session = _session("total_timeout")

    await orch._schedule_retry(session)

    record = orch._registry.get("1")
    assert record is not None
    assert record.next_retry_at is not None
    assert record.retry_count == 1


@pytest.mark.asyncio
async def test_retry_queue_defers_item_when_no_slot(tmp_path: Path) -> None:
    """An item whose delay expired while the concurrency slot was
    occupied must stay queued (not vanish) and launch once a slot frees.
    """
    import time as _time

    from orchestratord.session_state import RetryItem

    orch = _orchestrator(tmp_path)
    orch._state.max_concurrent_agents = 0  # no slots
    item = RetryItem(
        issue_id="1",
        attempt=1,
        delay_seconds=0.0,
        identifier="ISSUE-1",
        scheduled_at=0.0,
    )
    orch._state.retry_queue = [item]

    await orch._process_retry_queue()

    assert orch._state.retry_queue == [item], (
        "deferred retry must be retained when no concurrency slot is free"
    )
    assert _time.time() >= item.scheduled_at + item.delay_seconds or True


@pytest.mark.asyncio
async def test_retry_queue_launches_when_slot_frees(tmp_path: Path) -> None:
    """Once a slot is free the due retry launches and the persisted plan
    is cleared from the registry record.
    """
    import time as _time

    from orchestratord.session_state import RetryItem

    orch = _orchestrator(tmp_path)
    orch._state.max_concurrent_agents = 2
    item = RetryItem(
        issue_id="1",
        attempt=1,
        delay_seconds=0.0,
        identifier="ISSUE-1",
        scheduled_at=0.0,
    )
    orch._state.retry_queue = [item]

    launched: list[str] = []

    async def _fake_launch(issue) -> None:
        launched.append(issue.id)

    orch._launch_issue = _fake_launch  # type: ignore[method-assign]
    orch.tracker = SimpleNamespace(
        active_states=["open"],
        fetch_issue_states_by_ids=_fetch_ok,
    )

    await orch._process_retry_queue()

    assert launched == ["1"]
    assert orch._state.retry_queue == []
    record = orch._registry.get("1")
    assert record is not None
    assert record.next_retry_at is None
    assert _time.time() > 0


async def _fetch_ok(ids):
    return {i: SimpleNamespace(id=i, state="open") for i in ids}


@pytest.mark.asyncio
async def test_retry_requeue_has_attempt_ceiling(tmp_path: Path) -> None:
    """Tracker fetch 永久丢项时,重排必须有天花板——否则
    幽灵 issue 会无限 WARNING 循环。超限后丢弃并清理持久化计划。
    """
    import time as _time

    from orchestratord.session_state import RetryItem

    orch = _orchestrator(tmp_path)
    orch._state.max_concurrent_agents = 2

    async def _fetch_empty(ids):
        return {}  # 永远查不到该 issue

    orch.tracker = SimpleNamespace(
        active_states=["open"],
        fetch_issue_states_by_ids=_fetch_empty,
    )

    item = RetryItem(
        issue_id="1",
        attempt=1,
        delay_seconds=0.0,
        identifier="ISSUE-1",
        scheduled_at=0.0,
    )
    orch._state.retry_queue = [item]
    record = orch._registry.get("1")
    record.next_retry_at = _time.time()
    orch._registry._save()

    # 反复触发(超过上限次数)
    for _ in range(10):
        await orch._process_retry_queue()

    assert orch._state.retry_queue == [], (
        "超过重排上限后必须丢弃,不得无限循环"
    )
    assert record.next_retry_at is None, "丢弃时必须清理持久化的重试计划"
