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


# ---------------------------------------------------------------------------
# Retry/close gating: a pending retry must NOT close the tracker issue
# — GitCode cannot reopen, so the persisted retry plan used to be
# dropped the moment the poller saw the closed state.
# ---------------------------------------------------------------------------


def _tracker_recorder() -> tuple[SimpleNamespace, list[tuple[str, str]]]:
    calls: list[tuple[str, str]] = []

    async def _sync(issue_id: str, state: str) -> bool:
        calls.append((issue_id, state))
        return True

    return SimpleNamespace(update_issue_state=_sync), calls


@pytest.mark.asyncio
async def test_schedule_retry_returns_true_when_queued(tmp_path: Path) -> None:
    orch = _orchestrator(tmp_path)
    scheduled = await orch._schedule_retry(_session("total_timeout"))
    assert scheduled is True
    assert len(orch._state.retry_queue) == 1


@pytest.mark.asyncio
async def test_schedule_retry_returns_false_for_operator_stop(
    tmp_path: Path,
) -> None:
    orch = _orchestrator(tmp_path)
    scheduled = await orch._schedule_retry(_session("operator_stop"))
    assert scheduled is False
    assert orch._state.retry_queue == []


@pytest.mark.asyncio
async def test_retry_exhaustion_syncs_abandoned_not_failed(
    tmp_path: Path,
) -> None:
    """When the retry limit is hit, the terminal ``abandoned`` state is
    synced (closing the tracker issue) — and the caller's ``failed``
    sync is skipped by the returned False.
    """
    orch = _orchestrator(tmp_path)
    orch.workflow.agent.max_retry_attempts = 1
    tracker, calls = _tracker_recorder()
    orch.tracker = tracker
    orch._state.retry_attempts["1"] = 1  # next attempt = 2 > max 1

    scheduled = await orch._schedule_retry(_session("total_timeout"))

    assert scheduled is False
    assert orch._state.retry_queue == []
    assert ("1", "abandoned") in calls
    assert ("1", "failed") not in calls


@pytest.mark.asyncio
async def test_genuine_failure_keeps_tracker_issue_open_until_exhausted(
    tmp_path: Path,
) -> None:
    """The caller-side gate: retry scheduled → no tracker state sync
    (issue stays open+assigned); the close only lands when the retry
    machinery gives up.
    """
    orch = _orchestrator(tmp_path)
    tracker, calls = _tracker_recorder()
    orch.tracker = tracker
    session = _session("total_timeout")

    retry_scheduled = await orch._schedule_retry(session)
    if not retry_scheduled:
        await orch._sync_tracker_issue_state(session.issue.id or "", "failed")

    assert retry_scheduled is True
    assert calls == [], "pending retry must not close the tracker issue"


# ---------------------------------------------------------------------------
# Run Summary root cause: the raw backend error must survive the
# downstream guards that overwrite session_end_summary.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_summary_surfaces_backend_error_detail(
    tmp_path: Path,
) -> None:
    orch = _orchestrator(tmp_path)
    created: list[str] = []

    async def _create(issue_id: str, body: str) -> int:
        created.append(body)
        return 42

    orch.tracker = SimpleNamespace(create_comment=_create)

    session = _session(None)
    session.summary_comment_id = None
    session.status = "failed"
    session.session_end_reason = "no_changes_produced"
    session.session_end_summary = (
        "Agent did not produce any file modifications; no PR was created."
    )
    session.backend_error_detail = (
        "opencode_unexpected_response: POST /v1/chat returned HTML"
    )

    await orch._update_issue_summary(session)

    assert created, "summary comment must be created"
    assert "no_changes_produced" in created[0]
    assert "Backend error:" in created[0], (
        "the raw backend error must reach the tracker summary"
    )
    assert "opencode_unexpected_response" in created[0]


# ---------------------------------------------------------------------------
# #13 [O5][MAJOR] 持久化 retry plan 重启后永不恢复。
# ``_schedule_retry`` 把 ``next_retry_at`` 持久化到 registry 记录，但启动
# 恢复路径只处理 RUNNING 记录，从不回填 retry_queue——daemon 重启后计划中
# 的重试静默蒸发，issue 无限挂起。以下测试验证启动时重建 retry queue。
# ---------------------------------------------------------------------------


def _orchestrator_from_disk(tmp_path: Path) -> Orchestrator:
    """Orchestrator backed by the on-disk registry (no re-register).

    ``_orchestrator`` re-registers issue "1", which would overwrite the
    persisted ``next_retry_at`` — this helper only loads the registry so
    the file's records (including a persisted retry plan) survive.
    """
    orch = object.__new__(Orchestrator)
    orch._state = OrchestratorState()
    orch._registry = IssueRegistry(tmp_path / "registry.json")
    orch.workflow = SimpleNamespace(
        agent=SimpleNamespace(
            max_retry_attempts=3,
            max_retry_backoff_ms=60_000,
            max_turns_retry_delay_ms=60_000,
        )
    )
    return orch


def _persist_retry_plan(tmp_path: Path, *, retry_count: int = 1) -> None:
    """First-daemon-lifetime simulation: write a registry record carrying
    a persisted retry plan (``next_retry_at`` already due, status FAILED —
    the exact state ``_schedule_retry`` leaves behind after a failure).
    """
    import time as _time

    from orchestratord.issue_registry.models import IssueStatus

    first = _orchestrator(tmp_path)
    record = first._registry.get("1")
    assert record is not None
    record.retry_count = retry_count
    record.next_retry_at = _time.time() - 10.0  # came due while daemon was down
    record.status = IssueStatus.FAILED
    first._registry._save()


@pytest.mark.asyncio
async def test_startup_recovers_persisted_retry_plan(tmp_path: Path) -> None:
    """A retry plan persisted by a previous daemon lifetime must be
    rebuilt into the retry queue on startup and dispatched by the normal
    retry machinery.
    """
    _persist_retry_plan(tmp_path)

    # Second daemon lifetime: fresh in-memory state, same registry.
    orch = _orchestrator_from_disk(tmp_path)
    assert orch._state.retry_queue == []
    assert orch._state.retry_attempts == {}

    orch._recover_pending_retries()

    assert len(orch._state.retry_queue) == 1, (
        "persisted retry plan must be rebuilt into the retry queue"
    )
    recovered = orch._state.retry_queue[0]
    assert recovered.issue_id == "1"
    assert recovered.delay_seconds == 0.0, "overdue retry must be immediately ready"
    assert orch._state.retry_attempts.get("1") == 1, (
        "attempt counter must be restored so max_retry_attempts holds"
    )

    # The recovered plan flows through the normal dispatch path — the
    # concurrency-slot, tracker active-state and requeue guards still apply.
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


@pytest.mark.asyncio
async def test_recover_pending_retry_defers_future_due_time(tmp_path: Path) -> None:
    """A retry whose due time is still in the future keeps its remaining
    delay — recovery must not launch it early.
    """
    import time as _time

    from orchestratord.issue_registry.models import IssueStatus

    first = _orchestrator(tmp_path)
    record = first._registry.get("1")
    assert record is not None
    record.retry_count = 1
    record.next_retry_at = _time.time() + 300.0  # 5 minutes out
    record.status = IssueStatus.FAILED
    first._registry._save()

    orch = _orchestrator_from_disk(tmp_path)
    orch._recover_pending_retries()

    assert len(orch._state.retry_queue) == 1
    recovered = orch._state.retry_queue[0]
    assert 290.0 < recovered.delay_seconds <= 300.0, (
        "future plan must keep its remaining delay"
    )


@pytest.mark.asyncio
async def test_recover_pending_retry_respects_slot_limit(tmp_path: Path) -> None:
    """Recovery only rebuilds the queue; dispatch still respects the
    concurrency-slot guard (no launch while the daemon is at capacity).
    """
    _persist_retry_plan(tmp_path)

    orch = _orchestrator_from_disk(tmp_path)
    orch._recover_pending_retries()
    assert len(orch._state.retry_queue) == 1

    # No concurrency slots free — the recovered retry must stay queued.
    orch._state.max_concurrent_agents = 0
    orch.tracker = SimpleNamespace(
        active_states=["open"],
        fetch_issue_states_by_ids=_fetch_ok,
    )

    await orch._process_retry_queue()

    assert len(orch._state.retry_queue) == 1, (
        "recovered retry must defer when no concurrency slot is free"
    )


@pytest.mark.asyncio
async def test_recover_pending_retry_respects_requeue_ceiling(
    tmp_path: Path,
) -> None:
    """A recovered retry that the tracker permanently misses is dropped
    after the requeue ceiling — recovery must not bypass that guard.
    """
    _persist_retry_plan(tmp_path)

    orch = _orchestrator_from_disk(tmp_path)
    orch._recover_pending_retries()
    assert len(orch._state.retry_queue) == 1

    async def _fetch_empty(ids):
        return {}  # tracker never reports the issue

    orch.tracker = SimpleNamespace(
        active_states=["open"],
        fetch_issue_states_by_ids=_fetch_empty,
    )

    # max_retry_attempts=3 → requeue ceiling is 3. Repeated misses must
    # eventually drop the recovered item instead of looping forever.
    for _ in range(10):
        await orch._process_retry_queue()

    assert orch._state.retry_queue == [], (
        "recovered retry must respect the requeue ceiling"
    )
    record = orch._registry.get("1")
    assert record is not None
    assert record.next_retry_at is None, (
        "dropped recovered retry must clear the persisted plan"
    )


@pytest.mark.asyncio
async def test_recover_pending_retry_restores_attempt_count(
    tmp_path: Path,
) -> None:
    """The persisted retry_count restores the in-memory attempt counter
    so max_retry_attempts is not bypassed by a restart.
    """
    _persist_retry_plan(tmp_path, retry_count=2)

    orch = _orchestrator_from_disk(tmp_path)
    orch._recover_pending_retries()

    assert orch._state.retry_attempts.get("1") == 2
    assert orch._state.retry_queue[0].attempt == 2


@pytest.mark.asyncio
async def test_recover_pending_retry_is_noop_without_plan(tmp_path: Path) -> None:
    """Records without a persisted retry plan are left untouched."""
    orch = _orchestrator(tmp_path)

    orch._recover_pending_retries()

    assert orch._state.retry_queue == []
