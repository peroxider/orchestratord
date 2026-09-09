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
    # issue_id promoted on all three events (error/usage/session_end).
    assert summary["by_issue"]["42"] == 3

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


def test_empty_day_renders_base_tables_only(telemetry_home) -> None:
    from orchestratord.telemetry.aggregator import aggregate_day, render_summary_markdown

    summary = aggregate_day()
    assert summary["events"] == 0
    assert summary["session_duration"]["count"] == 0

    rendered = render_summary_markdown(summary)
    assert "## 汇总" in rendered
    assert "## 耗时 (agent 会话)" not in rendered
