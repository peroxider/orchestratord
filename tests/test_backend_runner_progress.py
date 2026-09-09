"""Regression tests for BackendRunner progress event adaptation."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from orchestratord.kernel.approval import ToolCallEvent, get_approval_policy
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
async def test_backend_error_cannot_be_overwritten_by_success_terminal() -> None:
    """A fatal backend event must remain visible and fail the run.

    Some adapters emit their terminal event from ``finally``.  A success
    terminal after a spawn/protocol error must not turn that failed session
    back into a successful one.
    """
    runner = object.__new__(BackendRunner)
    runner._check_file_changes = lambda *_args: _changed()  # type: ignore[method-assign]
    reporter = _ProgressReporter()
    session = SimpleNamespace(
        turn_count=0,
        tool_count=0,
        status="running",
        session_end_reason=None,
        session_end_summary=None,
        control_socket=None,
    )
    spi_session = _Session(
        [
            EventEnvelope(
                seq=1,
                timestamp=0,
                kind=EventKind.ERROR,
                payload={
                    "code": "codex_spawn_error",
                    "message": "failed to spawn worker: codex not found",
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
        reporter,
    )

    assert session.status == "failed"
    assert session.session_end_reason == "backend_error"
    assert session.session_end_summary == "failed to spawn worker: codex not found"
    assert reporter.completions == [(SessionComplete(reason="backend_error"), session)]


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
    assert "66---修复" in run_id


def test_run_id_has_entropy_for_same_second_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    """Two runs of the same issue within the same second must not collide.

    Regression for the issue-path ``_build_run_id`` which used to return a
    second-precision ``{ts}_{slug}`` with no entropy — a manual requeue or
    fast retry in the same second produced the same run_id and the stage
    transcript / session directory overwrote each other.
    """
    from orchestratord import backend_runner

    # Freeze time so the timestamp portion of both calls is identical.
    frozen = datetime(2026, 9, 8, 12, 0, 0, tzinfo=UTC)

    class _FrozenDT:
        UTC = UTC

        @classmethod
        def now(cls, tz=None):
            return frozen

    monkeypatch.setattr(backend_runner, "datetime", _FrozenDT)

    session = SimpleNamespace(issue=SimpleNamespace(identifier="issue-42"))
    run_id_1 = BackendRunner._build_run_id(session)
    run_id_2 = BackendRunner._build_run_id(session)

    assert run_id_1 != run_id_2, (
        "same-second run_ids must differ; "
        "entropy suffix is missing from _build_run_id"
    )
    assert run_id_1.startswith("20260908_120000_issue-42-")
    assert run_id_2.startswith("20260908_120000_issue-42-")


async def _changed() -> bool:
    return True


async def test_session_complete_extracts_cost_and_usage() -> None:
    """The core must read cost telemetry from the
    SESSION_COMPLETE payload (clawcodex/claude convention:
    ``total_cost_usd``; dsh convention: ``usage`` token dict) instead of
    ignoring it. Backend token keys are normalized to the canonical
    lowercase format (``input``/``output``/``reasoning``/``cache_read``)
    so the registry / CLI / dashboard consume a single consistent shape.
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
    assert session.token_usage == {"input": 150, "output": 30}


async def test_session_complete_usage_reaches_diagnostics_callback() -> None:
    """Regression (#12): the usage extracted from SESSION_COMPLETE must
    reach the run-diagnostics callback.

    The in-loop diagnostics callback fires BEFORE each event is
    processed, so the last in-loop invocation cannot see the
    ``token_usage`` that the SESSION_COMPLETE handler sets before
    breaking. ``_process_events`` must invoke the callback once more
    after the loop ends, so the registry (the persistent diagnostics
    source behind ``issue show`` / telemetry) receives the real token
    numbers instead of an empty dict.
    """
    import tempfile
    from pathlib import Path

    from orchestratord.issue_registry import IssueRegistry

    runner = object.__new__(BackendRunner)
    runner._check_file_changes = lambda *_args: _changed()  # type: ignore[method-assign]

    with tempfile.TemporaryDirectory() as tmp:
        reg_path = Path(tmp) / "registry.json"
        reg = IssueRegistry(reg_path)
        reg.register(issue_id="1", issue_identifier="ISSUE-1")
        reg.mark_running("1")

        seen_usage: list[dict] = []

        def diagnostics_callback(sess: Any) -> None:
            issue_id = getattr(getattr(sess, "issue", None), "id", None) or ""
            rec = reg.update_run_diagnostics(
                issue_id,
                run_id=getattr(sess, "run_id", None),
                turn_count=getattr(sess, "turn_count", 0),
                tool_count=getattr(sess, "tool_count", 0),
                token_usage=getattr(sess, "token_usage", None),
                cost_usd=getattr(sess, "cost_usd", None),
            )
            if rec is not None:
                seen_usage.append(dict(rec.run_token_usage))

        session = SimpleNamespace(
            turn_count=0,
            tool_count=0,
            status="running",
            session_end_reason=None,
            control_socket=None,
            cost_usd=0.0,
            token_usage={},
            run_id="run-1",
            issue=SimpleNamespace(id="1"),
        )
        spi_session = _Session(
            [
                EventEnvelope(
                    seq=1,
                    timestamp=0,
                    kind=EventKind.SESSION_COMPLETE,
                    payload={
                        "reason": "success",
                        "usage": {
                            "inputTokens": 4287,
                            "outputTokens": 34,
                            "reasoningTokens": 120,
                            "cacheReadTokens": 500,
                        },
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
            None,
            diagnostics_callback=diagnostics_callback,
        )

        # _process_events must persist the terminal usage itself — no
        # orchestrator finally-block call required.
        assert session.token_usage == {
            "input": 4287,
            "output": 34,
            "reasoning": 120,
            "cache_read": 500,
        }
        assert seen_usage, "diagnostics callback was never invoked"
        assert seen_usage[-1] == session.token_usage, (
            f"last diagnostics snapshot has empty/old usage: {seen_usage[-1]}"
        )

        # Reload from disk: the record's run_token_usage must carry the
        # real token counts.
        reloaded = IssueRegistry(reg_path).get("1")
        assert reloaded is not None
        assert reloaded.run_token_usage == {
            "input": 4287,
            "output": 34,
            "reasoning": 120,
            "cache_read": 500,
        }, f"registry run_token_usage empty: {reloaded.run_token_usage}"


async def test_session_complete_no_usage_stays_empty() -> None:
    """Regression (#12) criterion 3: when the backend reports no usage,
    the registry must keep ``run_token_usage`` empty — never fabricate
    zeros or estimates.
    """
    import tempfile
    from pathlib import Path

    from orchestratord.issue_registry import IssueRegistry

    runner = object.__new__(BackendRunner)
    runner._check_file_changes = lambda *_args: _changed()  # type: ignore[method-assign]

    with tempfile.TemporaryDirectory() as tmp:
        reg_path = Path(tmp) / "registry.json"
        reg = IssueRegistry(reg_path)
        reg.register(issue_id="1", issue_identifier="ISSUE-1")
        reg.mark_running("1")

        def diagnostics_callback(sess: Any) -> None:
            issue_id = getattr(getattr(sess, "issue", None), "id", None) or ""
            reg.update_run_diagnostics(
                issue_id,
                run_id=getattr(sess, "run_id", None),
                turn_count=getattr(sess, "turn_count", 0),
                tool_count=getattr(sess, "tool_count", 0),
                token_usage=getattr(sess, "token_usage", None),
                cost_usd=getattr(sess, "cost_usd", None),
            )

        session = SimpleNamespace(
            turn_count=0,
            tool_count=0,
            status="running",
            session_end_reason=None,
            control_socket=None,
            cost_usd=0.0,
            token_usage={},
            run_id="run-1",
            issue=SimpleNamespace(id="1"),
        )
        spi_session = _Session(
            [
                EventEnvelope(
                    seq=1,
                    timestamp=0,
                    kind=EventKind.SESSION_COMPLETE,
                    payload={"reason": "success"},  # no usage key
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
            diagnostics_callback=diagnostics_callback,
        )

        assert session.token_usage == {}
        reloaded = IssueRegistry(reg_path).get("1")
        assert reloaded is not None
        assert reloaded.run_token_usage == {}, (
            f"expected empty run_token_usage, got {reloaded.run_token_usage}"
        )


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
