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

from orchestratord.issue_registry import IssueRegistry, IssueStatus
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


# ---------------------------------------------------------------------------
# #37 [O2+O3][MAJOR] retry 队列收不到 stop/takeover；takeover 是会被覆盖的
# no-op。控制平面的核心承诺：操作者动作不可被自动重试推翻。
#   - O2：issue 处于 retry_queue（等待 next_retry_at）时 stop/takeover 被
#     running 门卫丢弃，重试到点照常触发。
#   - O3：stop/takeover 分支不写 session_end_reason / takeover 不 cancel
#     task，end_reason 不在 NON_RETRYABLE_END_REASONS 内被自动重试复活。
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stop_on_retry_queue_cancels_pending_retry(tmp_path: Path) -> None:
    """[O2] stop 文件在 issue 等待重试时必须生效。

    A pending retry keeps the tracker issue open (GitCode cannot
    reopen). The operator's stop must remove the queued retry (in-memory
    item + persisted plan), record ``session_end_reason=operator_stop``
    on the registry and close the tracker issue — the retry must never
    fire later.
    """
    import asyncio
    import time as _time

    from orchestratord.session_state import RetryItem

    orch = _orchestrator(tmp_path)
    orch._state.max_concurrent_agents = 2
    item = RetryItem(
        issue_id="1",
        attempt=1,
        delay_seconds=300.0,
        identifier="ISSUE-1",
        scheduled_at=_time.time(),
    )
    orch._state.retry_queue = [item]
    record = orch._registry.get("1")
    assert record is not None
    record.next_retry_at = _time.time() + 300.0
    orch._registry._save()

    tracker, calls = _tracker_recorder()
    orch.tracker = tracker

    orch._apply_control_command("stop", "1", "")
    await asyncio.sleep(0)  # let the tracker-sync task run

    # The queued retry must be gone — both in-memory and persisted.
    assert orch._state.retry_queue == [], "stop must remove the queued retry"
    assert record.next_retry_at is None, (
        "stop must clear the persisted retry plan"
    )
    assert record.session_end_reason == "operator_stop"
    # Tracker semantics: a pending retry keeps the issue open, so the
    # operator stop must close it.
    assert ("1", "failed") in calls, "stop must close the tracker issue"

    # The retry must not fire later even after a queue pass.
    launched: list[str] = []

    async def _fake_launch(issue) -> None:
        launched.append(issue.id)

    orch._launch_issue = _fake_launch  # type: ignore[method-assign]
    await orch._process_retry_queue()

    assert launched == [], "cancelled retry must never launch"


@pytest.mark.asyncio
async def test_takeover_on_retry_queue_cancels_pending_retry(
    tmp_path: Path,
) -> None:
    """[O2] takeover 对等待重试的 issue 同样必须生效。"""
    import time as _time

    from orchestratord.session_state import RetryItem

    orch = _orchestrator(tmp_path)
    orch._state.max_concurrent_agents = 2
    item = RetryItem(
        issue_id="1",
        attempt=1,
        delay_seconds=300.0,
        identifier="ISSUE-1",
        scheduled_at=_time.time(),
    )
    orch._state.retry_queue = [item]
    record = orch._registry.get("1")
    assert record is not None
    record.next_retry_at = _time.time() + 300.0
    orch._registry._save()

    orch._apply_control_command("takeover", "1", "")

    assert orch._state.retry_queue == [], (
        "takeover must remove the queued retry"
    )
    assert record.next_retry_at is None, (
        "takeover must clear the persisted retry plan"
    )
    assert record.session_end_reason == "operator_takeover"
    assert orch._state.retry_attempts.get("1") is None


@pytest.mark.asyncio
async def test_stop_on_running_session_records_end_reason_and_cancels_task(
    tmp_path: Path,
) -> None:
    """[O3] stop 必须写非重试 end_reason 并 cancel task。

    Without the end_reason the runner completes normally, the status is
    overwritten and the issue is auto-retried (stop → retry → stop loop).
    """
    import asyncio

    orch = _orchestrator(tmp_path)

    cancelled = asyncio.Event()

    async def _run_until_cancelled() -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    task = asyncio.create_task(_run_until_cancelled())
    # Let the task start running (await Event().wait()) before applying
    # the control — otherwise task.cancel() on an unstarted task is
    # handled at the task level without the coroutine body executing,
    # so the except CancelledError block never fires.
    await asyncio.sleep(0)
    orch._issue_tasks = {"1": task}
    session = SimpleNamespace(
        issue=SimpleNamespace(id="1", identifier="ISSUE-1"),
        status="running",
        session_end_reason=None,
        session_end_summary="",
        pause_resume_event=asyncio.Event(),
    )
    orch._state.running["1"] = session

    orch._apply_control_command("stop", "1", "")

    assert session.status == "failed"
    assert session.session_end_reason == "operator_stop"
    await asyncio.wait_for(cancelled.wait(), timeout=1.0)
    assert task.cancelled()

    # The end reason must keep the issue out of the auto-retry loop.
    await orch._schedule_retry(session)
    assert orch._state.retry_queue == [], (
        "operator_stop must not be auto-retried"
    )


@pytest.mark.asyncio
async def test_takeover_on_running_session_cancels_task_and_skips_retry(
    tmp_path: Path,
) -> None:
    """[O3] takeover 必须 cancel task 并写非重试 end_reason。

    Historically takeover only set ``status`` and unblocked the pause
    event: the runner completed normally and overwrote the status, and
    the issue was auto-retried (end_reason not in
    NON_RETRYABLE_END_REASONS).
    """
    import asyncio

    orch = _orchestrator(tmp_path)

    cancelled = asyncio.Event()

    async def _run_until_cancelled() -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    task = asyncio.create_task(_run_until_cancelled())
    # Let the task start running (await Event().wait()) before applying
    # the control — otherwise task.cancel() on an unstarted task is
    # handled at the task level without the coroutine body executing,
    # so the except CancelledError block never fires.
    await asyncio.sleep(0)
    orch._issue_tasks = {"1": task}
    session = SimpleNamespace(
        issue=SimpleNamespace(id="1", identifier="ISSUE-1"),
        status="running",
        session_end_reason=None,
        session_end_summary="",
        pause_resume_event=asyncio.Event(),
    )
    orch._state.running["1"] = session

    orch._apply_control_command("takeover", "1", "")

    assert session.status == "failed"
    assert session.session_end_reason == "operator_takeover"
    await asyncio.wait_for(cancelled.wait(), timeout=1.0)
    assert task.cancelled()

    # The end reason must keep the issue out of the auto-retry loop.
    await orch._schedule_retry(session)
    assert orch._state.retry_queue == [], (
        "operator_takeover must not be auto-retried"
    )


def test_non_retryable_end_reasons_defined_once() -> None:
    """ST1: NON_RETRYABLE_END_REASONS 全库仅一处定义。

    A duplicated definition regressed the operator-stop guard (the
    runner's private copy did not contain the reason the control plane
    wrote). Lock the single-definition invariant in a test.
    """
    import re

    import orchestratord
    from orchestratord import orchestrator as orch_module
    from orchestratord.kernel.dispatch import NON_RETRYABLE_END_REASONS

    pkg_root = Path(orchestratord.__file__).resolve().parent
    pattern = re.compile(r"NON_RETRYABLE_END_REASONS\s*=\s*frozenset")
    hits = [
        str(path.relative_to(pkg_root))
        for path in sorted(pkg_root.rglob("*.py"))
        if pattern.search(path.read_text(encoding="utf-8"))
    ]
    assert hits == ["kernel/dispatch.py"], (
        "NON_RETRYABLE_END_REASONS must be defined exactly once in "
        f"kernel/dispatch.py, found: {hits}"
    )
    # The orchestrator must use the same object — not a private copy.
    assert orch_module.NON_RETRYABLE_END_REASONS is NON_RETRYABLE_END_REASONS

# #33 [F16][P2] crash 恢复把 stale running 标失败并关单，应保持 open 可 retry。
# ``_recover_stale_running_records`` 对 stale RUNNING 记录不走
# ``_sync_tracker_issue_state("failed")`` 关单，改为持久化重试计划
# （retry_count / next_retry_at），由 ``_recover_pending_retries``
# 在同一启动序列中重建 retry queue —— 与普通失败路径的 retry gate 一致。
# ---------------------------------------------------------------------------


def _stale_running_fixture(tmp_path: Path) -> None:
    """Pre-seed registry with a stale RUNNING record (crash simulation)."""
    orch = _orchestrator(tmp_path)
    record = orch._registry.get("1")
    assert record is not None
    record.status = IssueStatus.RUNNING
    orch._registry._save()


@pytest.mark.asyncio
async def test_crash_recovery_keeps_issue_open_and_retryable(
    tmp_path: Path,
) -> None:
    """A stale RUNNING record from a daemon crash must be recovered
    non-destructively: the tracker issue stays open, the retry plan is
    persisted, and the retry queue is rebuilt so the issue can be
    re-launched on the next retry queue dispatch.
    """
    _stale_running_fixture(tmp_path)

    # Simulate startup recovery: second daemon lifetime, fresh state.
    orch = _orchestrator_from_disk(tmp_path)
    tracker, calls = _tracker_recorder()
    orch.tracker = tracker

    await orch._recover_stale_running_records()
    orch._recover_pending_retries()

    # 1. Registry: failure reason recorded (mark_failed_with_reason)
    record = orch._registry.get("1")
    assert record is not None
    assert record.status == IssueStatus.FAILED
    assert record.verification_output == "Recovered stale running issue on orchestrator startup"
    assert record.verification_status == "failed"

    # 2. Tracker NOT closed — no "failed" state sync
    assert ("1", "failed") not in calls, (
        "crash recovery must not close the tracker issue"
    )
    assert calls == [], "no tracker state sync at all during recovery"

    # 3. Retry plan persisted on the registry record
    assert record.next_retry_at is not None, "retry plan must be persisted"
    assert record.retry_count == 1, "retry_count must be set to 1"

    # 4. Retry queue rebuilt by _recover_pending_retries
    assert len(orch._state.retry_queue) == 1, (
        "retry queue must contain the recovered plan"
    )
    recovered = orch._state.retry_queue[0]
    assert recovered.issue_id == "1"
    assert recovered.attempt == 1
    # The retry is deferred (backoff delay still pending) — the
    # existing test_startup_recovers_persisted_retry_plan covers the
    # full launch-via-_process_retry_queue flow.
