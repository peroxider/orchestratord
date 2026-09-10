"""Telemetry enrichment tests — runner emission + daily aggregator rollup.

All telemetry writes are redirected into a pytest tmp dir by pointing
``ORCHESTRATORD_HOME`` at it and reloading the storage module (its base
dir is resolved at import time).
"""

from __future__ import annotations

import importlib
import time
from types import SimpleNamespace
from typing import Any

import pytest

from orchestratord.backend_runner import BackendRunner
from orchestratord.spi.events import EventEnvelope, EventKind


@pytest.fixture
def telemetry_home(tmp_path, monkeypatch):
    monkeypatch.setenv("ORCHESTRATORD_HOME", str(tmp_path / "home"))
    from orchestratord.telemetry import storage

    importlib.reload(storage)
    yield storage
    importlib.reload(storage)


def _envelope(seq: int, kind: EventKind, **payload: Any) -> EventEnvelope:
    return EventEnvelope(seq=seq, timestamp=0, kind=kind, payload=payload)


class _SpiSession:
    def __init__(self, events: list[EventEnvelope]) -> None:
        self._events = events

    async def events(self):
        for event in self._events:
            yield event


def _session(**extra: Any) -> SimpleNamespace:
    base: dict[str, Any] = dict(
        turn_count=0,
        tool_count=0,
        status="running",
        session_end_reason=None,
        control_socket=None,
        backend_name="claude",
        _snapshot_model="claude-sonnet",
        session_id="sess-1",
        run_id="run-1",
        created_at=time.time() - 5.0,
        total_429_backoff_seconds=12.0,
        consecutive_429_count=2,
        cost_usd=0.0,
        token_usage={},
    )
    base.update(extra)
    return SimpleNamespace(**base)


def _runner() -> BackendRunner:
    runner = object.__new__(BackendRunner)

    async def _file_changed(*_args: Any) -> bool:
        return True

    runner._check_file_changes = _file_changed  # type: ignore[method-assign]
    runner._handle_tool_call_envelope = lambda *_args: None  # type: ignore[method-assign]
    return runner


@pytest.mark.asyncio
async def test_runner_emits_enriched_session_end(telemetry_home) -> None:
    spi = _SpiSession(
        [
            _envelope(1, EventKind.TOOL_CALL, name="Write", call_id="c1"),
            _envelope(2, EventKind.TOOL_RESULT, call_id="c1", is_error=True),
            _envelope(3, EventKind.TOOL_CALL, name="Read", call_id="c2"),
            _envelope(4, EventKind.TOOL_RESULT, call_id="c2"),
            _envelope(5, EventKind.TURN_COMPLETE, turn=1),
            _envelope(6, EventKind.TURN_COMPLETE, turn=2),
            _envelope(
                7,
                EventKind.SESSION_COMPLETE,
                reason="success",
                total_cost_usd=0.05,
                usage={"input": 100, "output": 50},
            ),
        ]
    )
    session = _session()

    await _runner()._process_events(spi, session, {}, None, None, None, None)

    from orchestratord.telemetry.aggregator import aggregate_day, render_summary_markdown

    summary = aggregate_day()

    # Session-level stats: one agent run, succeeded, queued ~5s.
    assert summary["sessions_ended"] == 1
    assert summary["sessions_succeeded"] == 1
    assert summary["session_duration"]["count"] == 1
    queue_avg = summary["latency"]["queue_wait_avg_s"]
    assert 4.0 < queue_avg < 15.0
    assert summary["session_e2e"]["count"] == 1
    assert summary["session_e2e"]["avg_s"] >= queue_avg
    assert summary["paused_s"] == 0.0
    assert summary["backoff_429_s"] == 12.0

    # Per-turn stats from the two TURN_COMPLETE events.
    assert summary["turns"]["turn_events"] == 2
    assert summary["turns"]["total"] == 2
    assert summary["turns"]["total_s"] >= 0.0

    # Per-backend rollup merges session_end + usage events.
    backend = summary["by_backend"]["claude"]
    assert backend["sessions"] == 1
    assert backend["succeeded"] == 1
    assert backend["turns"] == 2
    assert backend["tokens_input"] == 100
    assert backend["tokens_output"] == 50
    assert backend["cost_usd"] == pytest.approx(0.05)
    assert backend["duration_s"] > 0.0

    # Per-model rollup from the usage event.
    model = summary["by_model"]["claude-sonnet"]
    assert model["usage_events"] == 1
    assert model["tokens_input"] == 100

    # Per-tool rollup: Write failed once, Read succeeded once.
    assert summary["tools"]["Write"]["calls"] == 1
    assert summary["tools"]["Write"]["failures"] == 1
    assert summary["tools"]["Read"]["calls"] == 1
    assert summary["tools"]["Read"]["failures"] == 0

    rendered = render_summary_markdown(summary)
    assert "## 耗时 (agent 会话)" in rendered
    assert "## 按后端" in rendered
    assert "## 工具调用 Top 10" in rendered
    assert "## 按模型 (usage)" in rendered
    assert "claude" in rendered


@pytest.mark.asyncio
async def test_runner_records_only_first_backend_error(telemetry_home) -> None:
    spi = _SpiSession(
        [
            _envelope(
                1, EventKind.ERROR, code="spawn_fail", message="boom"
            ),
            _envelope(
                2, EventKind.ERROR, code="spawn_fail", message="boom again"
            ),
            _envelope(3, EventKind.SESSION_COMPLETE, reason="success"),
        ]
    )
    session = _session(issue=SimpleNamespace(id="42"))

    await _runner()._process_events(spi, session, {}, None, None, None, None)

    from orchestratord.telemetry.aggregator import aggregate_day

    summary = aggregate_day()

    # Only the first ERROR frame is recorded; the success terminal must
    # not erase the failure (backend_error end reason).
    assert summary["errors"] == 1
    assert summary["errors_by_reason"]["spawn_fail"] == 1
    assert summary["sessions_failed"] == 1
    assert summary["end_reasons_failed"]["backend_error"] == 1
    assert summary["by_backend"]["claude"]["failed"] == 1
    # issue_id promoted on all four events
    # (session_start/error/usage/session_end — the runner now emits
    # session_start per agent run).
    assert summary["by_issue"]["42"] == 4

    error_events = [
        ev for ev in telemetry_home.read_events() if ev.get("type") == "error"
    ]
    assert len(error_events) == 1
    assert error_events[0]["payload"]["message"] == "boom"
    assert error_events[0]["issue_id"] == "42"


def test_daemon_session_end_excluded_from_agent_stats(telemetry_home) -> None:
    from orchestratord.telemetry import record_session_end
    from orchestratord.telemetry.aggregator import aggregate_day, render_summary_markdown

    record_session_end(session_id="daemon", duration_s=86400, exit_status=0)

    summary = aggregate_day()

    # The daemon's own session_end has no turn_count key — counted in the
    # base stats but excluded from agent duration/backends.
    assert summary["sessions_ended"] == 1
    assert summary["sessions_succeeded"] == 1
    assert summary["session_duration"]["count"] == 0
    assert summary["by_backend"] == {}

    rendered = render_summary_markdown(summary)
    assert "## 耗时 (agent 会话)" not in rendered
    assert "## 按后端" not in rendered
    assert "| session 结束数 | 1 |" in rendered


def test_unattended_closed_loop_and_quartiles(telemetry_home) -> None:
    from orchestratord.telemetry import record_session_end
    from orchestratord.telemetry.aggregator import aggregate_day, render_summary_markdown

    def _agent_end(issue: str, *, ok: bool, reason: str, e2e: float) -> None:
        record_session_end(
            session_id=f"sess-{issue}-{e2e}",
            issue_id=issue,
            backend="opencode",
            turn_count=3,
            queue_wait_s=0.5,
            duration_s=e2e - 0.5,
            exit_status=0 if ok else 1,
            end_reason=reason,
        )

    # Issue 10: five unattended successes (e2e 1,1,1,1,2).
    for e2e in (1.0, 1.0, 1.0, 1.0, 2.0):
        _agent_end("10", ok=True, reason="success", e2e=e2e)
    # Issue 11: human takeover (failed) then an unattended retry success —
    # closed, but human-touched.
    _agent_end("11", ok=False, reason="operator_takeover", e2e=3.0)
    _agent_end("11", ok=True, reason="success", e2e=4.0)
    # Issue 12: backend error only — seen, never closed.
    _agent_end("12", ok=False, reason="backend_error", e2e=5.0)

    summary = aggregate_day()
    unattended = summary["unattended"]
    assert unattended["sessions_total"] == 8
    assert unattended["sessions_human"] == 1
    assert unattended["issues_seen"] == 3
    assert unattended["issues_closed"] == 2
    assert unattended["issues_unattended"] == 1
    assert unattended["closed_loop_rate"] == pytest.approx(2 / 3)
    assert unattended["rate"] == pytest.approx(1 / 3)

    # E2E ordered [1,1,1,1,2,3,4,5]: avg 2.25; quartile indexes via
    # round(q*(n-1)) → p25=1, p50=2, p75=3.
    e2e = summary["session_e2e"]
    assert e2e["count"] == 8
    assert e2e["avg_s"] == pytest.approx(2.25)
    assert e2e["p25_s"] == pytest.approx(1.0)
    assert e2e["p50_s"] == pytest.approx(2.0)
    assert e2e["p75_s"] == pytest.approx(3.0)
    assert summary["session_duration"]["p25_s"] == pytest.approx(0.5)
    assert summary["session_duration"]["p75_s"] == pytest.approx(2.5)

    rendered = render_summary_markdown(summary)
    assert "## 无人干预闭环" in rendered
    assert "| 无人干预闭环率 | 33.3% |" in rendered
    assert "| 闭环率 | 66.7% |" in rendered
    assert "| 人工干预会话数 | 1 |" in rendered
    assert "端到端 avg / Q1 / 中位 / Q3" in rendered
    assert "会话耗时 avg / Q1 / 中位 / Q3" in rendered


def test_closed_loop_efficiency_intervention_and_time_dimensions(
    telemetry_home,
) -> None:
    """闭环效率 / 干预分析 / 时间维度 — raw events with explicit ts."""
    from orchestratord.telemetry.storage import append_event
    from orchestratord.telemetry.aggregator import aggregate_day, render_summary_markdown

    base = time.mktime(time.strptime(time.strftime("%Y-%m-%d"), "%Y-%m-%d")) + 36000

    def _ev(etype, ts, sid, issue=None, payload=None, backend=None, model=None):
        ev: dict[str, Any] = {"type": etype, "ts": ts, "session_id": sid}
        if issue is not None:
            ev["issue_id"] = issue
        if backend is not None:
            ev["backend"] = backend
        if model is not None:
            ev["model"] = model
        if payload is not None:
            ev["payload"] = payload
        append_event(ev)

    # Issue 201: auto-failure then success — self-healed, closed loop
    # took 300s (first agent session end → first success).
    _ev("session_end", base + 60, "a1", "201",
        {"turn_count": 1, "duration_s": 50.0, "queue_wait_s": 10.0,
         "exit_status": 1, "end_reason": "backend_error"})
    _ev("session_end", base + 360, "a2", "201",
        {"turn_count": 2, "duration_s": 100.0, "queue_wait_s": 5.0,
         "exit_status": 0, "end_reason": "success",
         "tools": {"Bash": {"calls": 4, "failures": 1, "duration_ms": 800.0}}})
    _ev("usage", base + 370, "a2", "201",
        {"cost_usd": 0.60, "token_usage": {"inputTokens": 9000, "outputTokens": 100}},
        backend="claude", model="m1")
    # Issue 202: error event then a human takeover — settled, not
    # unattended-closed; the takeover has a prior error (干预前征兆).
    _ev("error", base + 100, "a3", "202", {"reason": "codex_spawn_error"})
    _ev("session_end", base + 200, "a3", "202",
        {"turn_count": 1, "duration_s": 30.0, "exit_status": 1,
         "end_reason": "operator_takeover"})
    # Issue 203: auto failure as its LAST session — still in progress.
    _ev("session_end", base + 300, "a4", "203",
        {"turn_count": 1, "duration_s": 15.0, "exit_status": 1,
         "end_reason": "idle_watchdog_timeout"})
    _ev("turn", base + 50, "a2", payload={"duration_s": 10.0})
    _ev("turn", base + 70, "a2", payload={"duration_s": 30.0})

    summary = aggregate_day()

    # 重试自愈: 2 automatic failures, only issue 201 recovered.
    assert summary["retry"]["retryable_failures"] == 2
    assert summary["retry"]["issues_self_healed"] == 1
    assert summary["retry"]["avg_retries_to_success"] == pytest.approx(1.0)

    # 闭环耗时 / 闭环成本.
    assert summary["closed_loop_time"]["count"] == 1
    assert summary["closed_loop_time"]["avg_s"] == pytest.approx(300.0)
    assert summary["cost"]["closed_issues"] == 1
    assert summary["cost"]["closed_total_usd"] == pytest.approx(0.60)
    assert summary["cost"]["closed_avg_usd"] == pytest.approx(0.60)
    assert summary["cost"]["top_issues"][0]["issue"] == "201"

    # 干预分析: the takeover had one prior error of a known reason.
    assert summary["intervention"]["human_sessions"] == 1
    assert summary["intervention"]["with_prior_errors"] == 1
    assert summary["intervention"]["avg_prior_errors"] == 1.0
    assert summary["intervention"]["prior_error_reasons"]["codex_spawn_error"] == 1

    # 口径修正: issue 203's last session auto-failed → in progress.
    unattended = summary["unattended"]
    assert unattended["issues_seen"] == 3
    assert unattended["issues_settled"] == 2
    assert unattended["issues_in_progress"] == 1
    assert unattended["rate_settled"] == pytest.approx(0.5)

    # 并发度: intervals a1[10,60] a2[260,360] a3[170,200] a4[285,300] —
    # a2 and a4 overlap, max 2; integral 195s over 350s span.
    assert summary["concurrency"]["max"] == 2
    assert summary["concurrency"]["avg"] == pytest.approx(195 / 350, rel=1e-3)

    # turn 耗时分布 + camelCase token normalization + tokens/turn.
    assert summary["turn_duration"]["count"] == 2
    assert summary["turn_duration"]["avg_s"] == pytest.approx(20.0)
    assert summary["tokens_input"] == 9000
    turns_total = summary["turns"]["total"]
    assert summary["tokens_input"] // turns_total == 1800

    # All 8 events fall in hour 10.
    assert summary["hourly"][10] == 8

    rendered = render_summary_markdown(summary)
    assert "## 闭环效率" in rendered
    assert "闭环耗时 avg / 中位 / p95 | 300.0s" in rendered
    assert "$0.6000" in rendered
    assert "自动失败次数 / 自愈 issue 数 / 平均重试到成功 | 2 / 1 / 1.0" in rendered
    assert "## 人工干预" in rendered
    assert "codex_spawn_error×1" in rendered
    assert "排除进行中 issue" in rendered
    assert "并发度 max / avg | 2" in rendered
    assert "turn 耗时 avg / Q1 / 中位 / Q3" in rendered
    assert "| tokens (in) / turn | 1800 |" in rendered
    assert "## 时段分布" in rendered
    assert "| 10:00-10:59 | 8 |" in rendered
    # 工具表新增失败率/均耗时列 (4 calls, 1 failure, 800ms total).
    assert "| 失败率 |" in rendered
    assert "| Bash | 4 | 1 | 25.0% | 800ms | 200ms |" in rendered


def test_empty_day_renders_base_tables_only(telemetry_home) -> None:
    from orchestratord.telemetry.aggregator import aggregate_day, render_summary_markdown

    summary = aggregate_day()
    assert summary["events"] == 0
    assert summary["session_duration"]["count"] == 0
    assert summary["unattended"]["issues_seen"] == 0
    assert summary["unattended"]["rate"] is None

    rendered = render_summary_markdown(summary)
    assert "## 汇总" in rendered
    assert "## 耗时 (agent 会话)" not in rendered
    assert "## 无人干预闭环" not in rendered


@pytest.mark.asyncio
async def test_verification_gate_records_blocked_and_passed(telemetry_home) -> None:
    from orchestratord.git.sync import GitSyncService
    from orchestratord.kernel.lifecycle import VerificationFailed

    async def _blocked(*_args: Any) -> None:
        raise VerificationFailed("test verification failed", "output")

    async def _passed(*_args: Any) -> None:
        pass

    session = _session(
        session_id=None,
        backend_session_id="bs-1",
        verification_status=None,
        issue=SimpleNamespace(id="7"),
    )
    runner = object.__new__(GitSyncService)
    runner._run_pre_push_verification_checks = _blocked  # type: ignore[method-assign]

    with pytest.raises(VerificationFailed):
        await runner._run_pre_push_verification("/tmp/repo", session)

    events = [ev for ev in telemetry_home.read_events() if ev.get("type") == "verification"]
    assert len(events) == 1
    assert events[0]["payload"]["outcome"] == "blocked"
    assert events[0]["payload"]["blocked_by"] == "test verification failed"
    # session_id falls back: session_id → backend_session_id → run_id.
    assert events[0]["session_id"] == "bs-1"
    assert events[0]["issue_id"] == "7"

    runner._run_pre_push_verification_checks = _passed  # type: ignore[method-assign]
    session.verification_status = "passed"
    await runner._run_pre_push_verification("/tmp/repo", session)

    events = [ev for ev in telemetry_home.read_events() if ev.get("type") == "verification"]
    assert len(events) == 2
    assert events[1]["payload"]["outcome"] == "passed"


def test_verification_and_crash_aggregation(telemetry_home) -> None:
    from orchestratord.telemetry import record_crash, record_verification
    from orchestratord.telemetry.aggregator import aggregate_day, render_summary_markdown

    record_verification(outcome="blocked", blocked_by="test verification failed")
    record_verification(outcome="passed")
    record_verification(outcome="blocked", blocked_by="repro verification failed")
    record_crash(kind="daemon_unclean_shutdown", previous_pid=123)
    record_crash(kind="backend_worker", detail="worker exited")

    summary = aggregate_day()

    gate = summary["verification"]
    assert gate["attempts"] == 3
    assert gate["blocked"] == 2
    assert gate["interception_rate"] == pytest.approx(2 / 3)
    assert gate["blocked_by"]["test verification failed"] == 1
    assert gate["blocked_by"]["repro verification failed"] == 1

    crashes = summary["crashes"]
    assert crashes["total"] == 2
    assert crashes["by_kind"]["daemon_unclean_shutdown"] == 1
    assert crashes["by_kind"]["backend_worker"] == 1
    # No agent sessions that day → per-session backend crash rate unknown.
    assert crashes["backend_rate_per_session"] is None

    rendered = render_summary_markdown(summary)
    assert "## 验证门" in rendered
    assert "缺陷拦截率 | 66.7%" in rendered
    assert "## 崩溃" in rendered
    assert "daemon_unclean_shutdown | 1 |" in rendered


def test_read_orchestrator_metadata(tmp_path, monkeypatch) -> None:
    import os

    from orchestratord import workspace_locator as wl

    monkeypatch.setattr(wl, "ORCHESTRATORD_ORCHESTRATOR_DIR", tmp_path)
    assert wl.read_orchestrator_metadata(tmp_path / "ws") is None

    wl.write_orchestrator_metadata(workspace_root=tmp_path / "ws", started_at=123.0)
    meta = wl.read_orchestrator_metadata(tmp_path / "ws")
    assert meta is not None
    assert meta["pid"] == os.getpid()
    assert meta["started_at"] == 123.0
