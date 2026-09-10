"""Runner-level follow-up tests for the DSH backend (selffix #38 D1).

The D1 regression: the old adapter emitted ``SESSION_COMPLETE`` after
every turn, so the runner's event loop broke on turn-1's terminal frame
before turn-2 events (from an operator follow-up) could be consumed —
the follow-up response was silently discarded and the run misreported
completed.

These tests verify the fixed lifecycle through the runner:

* ``test_runner_consumes_two_turns_with_followup`` drives the real
  ``DshSession`` (fake SDK harness, two scripted turns) through
  ``BackendRunner._process_events`` and asserts turn-2 text reaches
  ``session.output_text`` while exactly one ``SESSION_COMPLETE`` (from
  ``close()``) carries the cumulative usage.
* ``test_runner_merges_terminal_usage_once`` feeds a fixed-sequence fake
  session (two turns + one terminal ``SESSION_COMPLETE``) and asserts
  ``session.token_usage`` equals the real cumulative total — not doubled.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from orchestratord_dsh.session import DshSession

from orchestratord.backend_runner import BackendRunner
from orchestratord.issue_registry.issue import Issue
from orchestratord.session_state import RunSession, RunSubject
from orchestratord.spi.backend import SessionSpec
from orchestratord.spi.events import EventEnvelope, EventKind
from orchestratord.workspace import Workspace


def _notification(session_id: str, event: dict) -> SimpleNamespace:
    return SimpleNamespace(
        method="session.event",
        payload={"sessionId": session_id, "event": event},
    )


class _TwoTurnHarness:
    """Scripts two turns: first with text \"hello-from-turn-1\", second
    with text \"hello-from-turn-2\". Usage is reported per turn."""

    def __init__(self) -> None:
        self.started = False
        self.closed = False
        self._sent_followup = False

    def start(self) -> None:
        self.started = True

    def run(self, input: str, *, session_id=None, on_notification=None):
        if not self._sent_followup:
            # Turn 1
            self._sent_followup = True
            events = [
                {
                    "type": "assistant/message",
                    "data": {
                        "message": {"content": [{"type": "text", "text": "hello-from-turn-1"}]},
                        "usage": {"inputTokens": 100, "outputTokens": 50},
                    },
                },
                {"type": "turn/end", "data": {"reason": {"kind": "completed"}}},
            ]
        else:
            # Turn 2
            events = [
                {
                    "type": "assistant/message",
                    "data": {
                        "message": {"content": [{"type": "text", "text": "hello-from-turn-2"}]},
                        "usage": {"inputTokens": 200, "outputTokens": 80},
                    },
                },
                {"type": "turn/end", "data": {"reason": {"kind": "completed"}}},
            ]
        for event in events:
            if on_notification is not None:
                on_notification(_notification(session_id, event))
        return SimpleNamespace(
            session_id=session_id,
            final_response="",
            finish_reason="completed",
            events=events,
            notifications=[],
            session_root=None,
        )

    def close(self) -> None:
        self.closed = True


class _StatefulTracker:
    """Returns ``open`` for the first two turns (both follow-up turns run
    to completion and their SESSION_COMPLETE frames are consumed), then
    ``closed`` to terminate the run."""

    def __init__(self) -> None:
        self.calls = 0
        self.active_states = ["open"]

    async def fetch_issue_states_by_ids(
        self, issue_ids: list[str]
    ) -> dict[str, Issue]:
        self.calls += 1
        state = "open" if self.calls <= 2 else "closed"
        return {issue_id: Issue(id=issue_id, state=state) for issue_id in issue_ids}


def _session() -> RunSession:
    subject = RunSubject(id="test-followup-1", identifier="test-followup-1", title="test")
    ws = Workspace(path=Path("/tmp"), issue_identifier="test-followup-1")
    session = RunSession(subject=subject, workspace=ws)
    session._pending_followups = ["try alternative approach"]
    session.turn_count = 0
    session.tool_count = 0
    session.status = "running"
    session.output_text = ""
    session.token_usage = {}
    session.cost_usd = 0.0
    session.session_end_reason = None
    session.session_end_summary = None
    session.control_socket = None
    session.run_id = "run-test-followup"
    session.backend_name = "dsh"
    session._snapshot_model = "deepseek-v4-flash"
    session.created_at = 0.0
    session.started_at = None
    return session


class _FixedSequenceSession:
    """Fake spi_session mimicking the FIXED dsh lifecycle: two turns,
    then exactly one terminal SESSION_COMPLETE with cumulative usage."""

    def __init__(self) -> None:
        self.sent: list[str] = []

    async def events(self):
        for seq, (kind, payload) in enumerate(
            [
                (EventKind.TEXT, {"text": "hello-from-turn-1"}),
                (EventKind.TURN_COMPLETE, {"reason": "success"}),
                (EventKind.TEXT, {"text": "hello-from-turn-2"}),
                (EventKind.TURN_COMPLETE, {"reason": "success"}),
                (
                    EventKind.SESSION_COMPLETE,
                    {
                        "reason": "success",
                        "usage": {"inputTokens": 300, "outputTokens": 130},
                    },
                ),
            ],
            start=1,
        ):
            yield EventEnvelope(seq=seq, timestamp=0.0, kind=kind, payload=payload)

    async def send(self, content: str | list[Any]) -> None:
        self.sent.append(str(content))

    async def close(self) -> None:
        pass


@pytest.mark.asyncio
async def test_runner_consumes_two_turns_with_followup() -> None:
    """The runner must consume both turns' events when follow-up is
    queued; the single SESSION_COMPLETE from close() carries cumulative
    usage (regression: the old adapter's per-turn SESSION_COMPLETE broke
    the loop after turn-1 and discarded turn-2).
    """
    harness = _TwoTurnHarness()
    dsh = DshSession(
        SessionSpec(cwd="/tmp"),
        harness_factory=lambda: harness,
    )
    session = _session()
    tracker = _StatefulTracker()

    runner = object.__new__(BackendRunner)

    async def _changed(*_args: Any) -> bool:
        return True

    runner._check_file_changes = _changed  # type: ignore[method-assign]

    # Send the initial prompt and process events.
    await dsh.send("Original task prompt")
    await runner._process_events(
        dsh,
        session,
        session_context={},
        workflow=None,
        tracker=tracker,
        status_dashboard=None,
        progress_reporter=None,
        timeouts=None,
    )
    # The runner's finally block calls close() after _process_events.
    await dsh.close()

    # (a) turn-2 text must be in session.output_text.
    assert "hello-from-turn-1" in session.output_text, (
        "turn-1 text must reach session.output_text"
    )
    assert "hello-from-turn-2" in session.output_text, (
        "turn-2 text must reach session.output_text — the follow-up turn "
        "was discarded when a per-turn SESSION_COMPLETE broke the loop"
    )

    # (b) cumulative usage across both turns — not doubled.
    # The runner accumulates per-turn usage from each SESSION_COMPLETE
    # frame.  Turn 1 reports 100/50, turn 2 reports 200/80 → total 300/130.
    assert session.token_usage == {"input": 300, "output": 130}, (
        f"expected cumulative token_usage 300/130, got {session.token_usage}"
    )
    assert tracker.calls == 2, (
        f"tracker must be polled once per turn boundary, got {tracker.calls}"
    )


@pytest.mark.asyncio
async def test_runner_merges_terminal_usage_once() -> None:
    """When the runner processes the terminal SESSION_COMPLETE, it must
    set ``session.token_usage`` to the cumulative total exactly once —
    not accumulate on top of itself (the runner-side double-count fix).
    """
    spi = _FixedSequenceSession()
    session = _session()
    session._pending_followups = []  # follow-up is injected manually below

    runner = object.__new__(BackendRunner)

    async def _changed(*_args: Any) -> bool:
        return True

    runner._check_file_changes = _changed  # type: ignore[method-assign]

    # Queue a follow-up so the runner sends it at the turn-1 boundary.
    session._pending_followups = ["try alternative approach"]

    await runner._process_events(
        spi,
        session,
        session_context={},
        workflow=None,
        tracker=None,
        status_dashboard=None,
        progress_reporter=None,
        timeouts=None,
    )

    # The runner must have sent the follow-up prompt after turn-1.
    assert len(spi.sent) == 1, f"expected one follow-up send, got {spi.sent}"
    assert "try alternative approach" in spi.sent[0]

    # Both turns' text must be consumed.
    assert "hello-from-turn-1" in session.output_text
    assert "hello-from-turn-2" in session.output_text

    # token_usage = cumulative total, normalized to canonical lowercase keys.
    assert session.token_usage == {"input": 300, "output": 130}, (
        f"expected token_usage 300/130, got {session.token_usage}"
    )