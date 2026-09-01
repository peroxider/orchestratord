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
