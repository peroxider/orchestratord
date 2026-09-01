"""Regression tests for BackendRunner progress event adaptation."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from orchestratord.approval_policy import ToolCallEvent, get_approval_policy
from orchestratord.backend_runner import BackendRunner
from orchestratord.events.agent_events import SessionComplete, TurnComplete
from orchestratord.spi.approval import ApprovalDecision
from orchestratord.spi.events import EventEnvelope, EventKind


class _ProgressReporter:
    def __init__(self) -> None:
        self.turn_events: list[tuple[TurnComplete, Any]] = []
        self.completions: list[Any] = []

    def on_turn_complete(self, event: TurnComplete, session: Any) -> None:
        self.turn_events.append((event, session))

    def on_session_complete(self, event: Any, session: Any) -> None:
        self.completions.append((event, session))


class _Session:
    def __init__(self, events: list[EventEnvelope]) -> None:
        self._events = events
        self.approvals: list[tuple[str, ApprovalDecision]] = []

    async def events(self):
        for event in self._events:
            yield event

    async def approve(self, request_id: str, decision: ApprovalDecision) -> None:
        self.approvals.append((request_id, decision))


@pytest.mark.asyncio
async def test_turn_complete_uses_progress_event_contract() -> None:
    runner = object.__new__(BackendRunner)
    runner._check_file_changes = lambda *_args: _changed()  # type: ignore[method-assign]
    reporter = _ProgressReporter()
    session = SimpleNamespace(
        turn_count=0,
        tool_count=0,
        status="running",
        session_end_reason=None,
        control_socket=None,
    )
    spi_session = _Session(
        [
            EventEnvelope(
                seq=1,
                timestamp=0,
                kind=EventKind.TURN_COMPLETE,
                payload={"reason": "completed"},
            ),
            EventEnvelope(
                seq=2,
                timestamp=0,
                kind=EventKind.SESSION_COMPLETE,
                payload={"reason": "success"},
            ),
        ]
    )

    await runner._process_events(
        spi_session,
        session,
        {},
        None,
        None,
        None,
        reporter,
    )

    assert reporter.turn_events == [(TurnComplete(turn=1), session)]
    assert reporter.completions == [(SessionComplete(reason="success"), session)]
    assert session.status == "completed"


@pytest.mark.asyncio
async def test_approval_request_is_resolved_before_stream_continues() -> None:
    runner = object.__new__(BackendRunner)
    runner._approval_policy = get_approval_policy("approve-safe-only")
    session = SimpleNamespace(
        turn_count=0,
        tool_count=0,
        status="running",
        session_end_reason=None,
        control_socket=None,
    )
    spi_session = _Session(
        [
            EventEnvelope(
                seq=1,
                timestamp=0,
                kind=EventKind.APPROVAL_REQUEST,
                payload={
                    "request_id": "approval-1",
                    "call_id": "call-1",
                    "tool_name": "Bash",
                    "arguments": {"command": "rm important.txt"},
                },
            ),
            EventEnvelope(
                seq=2,
                timestamp=0,
                kind=EventKind.SESSION_COMPLETE,
                payload={"reason": "success"},
            ),
        ]
    )

    await runner._process_events(
        spi_session,
        session,
        {},
        None,
        None,
        None,
        None,
    )

    assert spi_session.approvals == [("approval-1", ApprovalDecision.DENY)]


def test_structured_reject_policy_fails_closed() -> None:
    policy = get_approval_policy(
        {
            "reject": {
                "sandbox_approval": True,
                "rules": True,
                "mcp_elicitations": True,
            }
        }
    )
    event = ToolCallEvent(tool_name="Bash", params={"command": "echo unsafe"})

    assert policy.evaluate(event, {}) is False
    assert event.is_approved is False


def test_run_id_sanitizes_tracker_punctuation_for_backend_transcripts() -> None:
    session = SimpleNamespace(issue=SimpleNamespace(identifier="#66 / 修复"))

    run_id = BackendRunner._build_run_id(session)

    assert all(char.isalnum() or char in "_-" for char in run_id)
    assert "#" not in run_id
    assert "/" not in run_id
    assert run_id.endswith("66---修复")


async def _changed() -> bool:
    return True


async def test_session_complete_extracts_cost_and_usage() -> None:
    """The core must read cost telemetry from the
    SESSION_COMPLETE payload (clawcodex/claude convention:
    ``total_cost_usd``; dsh convention: ``usage`` token dict) instead of
    ignoring it.
    """
    runner = object.__new__(BackendRunner)
    runner._check_file_changes = lambda *_args: _changed()  # type: ignore[method-assign]
    reporter = _ProgressReporter()
    session = SimpleNamespace(
        turn_count=0,
        tool_count=0,
        status="running",
        session_end_reason=None,
        control_socket=None,
        cost_usd=0.0,
        token_usage={},
    )
    spi_session = _Session(
        [
            EventEnvelope(
                seq=1,
                timestamp=0,
                kind=EventKind.SESSION_COMPLETE,
                payload={
                    "reason": "success",
                    "total_cost_usd": 0.42,
                    "usage": {"inputTokens": 150, "outputTokens": 30},
                },
            ),
        ]
    )

    await runner._process_events(
        spi_session,
        session,
        {},
        None,
        None,
        None,
        reporter,
    )

    assert session.cost_usd == 0.42
    assert session.token_usage == {"inputTokens": 150, "outputTokens": 30}


async def test_preflight_failure_short_circuits_run() -> None:
    """A backend that rejects the spec (e.g. provider without a
    runtime adapter) must fail the run BEFORE create_session, with the
    actionable message in session_end_summary.
    """
    from orchestratord.spi.backend import SessionSpec

    class _RejectingBackend:
        name = "dsh"
        display_name = "dsh"

        def preflight(self, spec: SessionSpec) -> None:
            raise RuntimeError(
                "provider 'anthropic' has no adapter in the DeepSeek Harness runtime"
            )

        def create_session(self, spec: SessionSpec) -> None:
            raise AssertionError("create_session must not run after preflight failure")

    runner = object.__new__(BackendRunner)
    runner.backend = _RejectingBackend()  # type: ignore[method-assign]
    session = SimpleNamespace(
        run_id="run-p6",
        status="running",
        session_end_reason=None,
        session_end_summary=None,
    )

    ok = runner._preflight_spec(SessionSpec(cwd="/w", provider="anthropic"), session)

    assert ok is False
    assert session.status == "failed"
    assert session.session_end_reason == "preflight_failed"
    assert "anthropic" in (session.session_end_summary or "")


async def test_preflight_pass_allows_run() -> None:
    """A backend whose preflight passes must not mark the session."""
    from orchestratord.spi.backend import SessionSpec

    class _OkBackend:
        name = "ok"
        display_name = "ok"

        def preflight(self, spec: SessionSpec) -> None:
            return None

    runner = object.__new__(BackendRunner)
    runner.backend = _OkBackend()  # type: ignore[method-assign]
    session = SimpleNamespace(
        run_id="run-p6",
        status="running",
        session_end_reason=None,
        session_end_summary=None,
    )

    ok = runner._preflight_spec(SessionSpec(cwd="/w"), session)

    assert ok is True
    assert session.status == "running"


async def test_run_task_generates_unique_run_id_per_run() -> None:
    """A deterministic task id (stage-01) must not become the
    run_id — dsh uses run_id as the harness session_id, so a second
    run of the same stage in the same workspace collided with the
    persisted session (id collision).
    """
    import copy as _copy

    from orchestratord.agent.task import AgentTask
    from orchestratord.config.schema import AgentConfig, SandboxConfig, WorkflowConfig

    runner = object.__new__(BackendRunner)
    runner.agent_config = AgentConfig()
    runner.sandbox_config = SandboxConfig()
    runner.workspace_cfg = None

    captured: list = []

    async def _fake_run(session, workflow, **kwargs):  # noqa: ANN001, ANN003
        captured.append(session)

    runner.run = _fake_run  # type: ignore[method-assign]

    task = AgentTask(id="stage-01", kind="workflow_stage", title="t", description="d")

    await runner.run_task(task)
    first_run_id = captured[0].run_id

    await runner.run_task(task)
    second_run_id = captured[1].run_id

    assert first_run_id != "stage-01", "run_id must not equal the deterministic task id"
    assert first_run_id != second_run_id, "each run needs a unique run_id"
    assert first_run_id.startswith("stage-01"), "stage linkage should be preserved"


def test_resolve_stage_run_id_tolerates_non_numeric_keys(tmp_path) -> None:
    """`max(stage_run_ids, key=int)` crashed on
    malformed (non-numeric) keys and the bare except silently degraded
    to the workflow run id. Malformed entries must be skipped instead.
    """
    from types import SimpleNamespace as _Ns

    from orchestratord.cli.run import _resolve_stage_run_id
    from orchestratord.run_store import RunRecord, RunStore

    ws = tmp_path / "ws"
    ws.mkdir()
    store = RunStore(ws)
    record = RunRecord(run_id="run-x", workflow="t", task_id="t", task_kind="generic")
    record.metadata = {
        "stage_run_ids": {"build": "stage-bad", "1": "stage-01-ok", "oops": None}
    }
    store.save(record)

    args = _Ns(id="run-x", workspace=str(ws))
    assert _resolve_stage_run_id(args) == "stage-01-ok"
