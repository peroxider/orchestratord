"""Regression tests for BackendRunner progress event adaptation."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from orchestratord.backend_runner import BackendRunner
from orchestratord.events.agent_events import SessionComplete, TurnComplete
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

    async def events(self):
        for event in self._events:
            yield event


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


async def _changed() -> bool:
    return True
