from __future__ import annotations

from typing import Any

import pytest

from orchestratord.spi.approval import ApprovalDecision
from orchestratord.spi.backend import SessionSpec
from orchestratord.spi.events import EventKind


@pytest.mark.asyncio
async def test_default_spec_does_not_override_query_timeout_defaults(
    monkeypatch,
) -> None:
    from extensions.api import query
    from orchestratord_clawcodex.session import ClawcodexSession

    captured: dict[str, Any] = {}

    def fake_config(**kwargs: Any) -> object:
        captured.update(kwargs)
        return object()

    class FakeRunner:
        def __init__(self, config: object) -> None:
            self.config = config

        async def stream(self):
            yield query.SessionComplete(reason="success")

        def approve(self, request_id: str, decision: Any) -> bool:
            return False

        def cancel_pending_approvals(self, message: str) -> None:
            return None

    monkeypatch.setattr(query, "QueryConfig", fake_config)
    monkeypatch.setattr(query, "QueryRunner", FakeRunner)
    session = ClawcodexSession(SessionSpec(cwd="/tmp"))
    try:
        await session.send("hello")
        events = [event async for event in session.events()]
    finally:
        await session.close()

    assert events[-1].kind is EventKind.SESSION_COMPLETE
    assert "timeout_s" not in captured
    assert "stall_timeout_s" not in captured
    assert "stall_warn_s" not in captured


@pytest.mark.asyncio
async def test_explicit_spi_timeouts_are_forwarded(monkeypatch) -> None:
    from extensions.api import query
    from orchestratord_clawcodex.session import ClawcodexSession

    captured: dict[str, Any] = {}

    def fake_config(**kwargs: Any) -> object:
        captured.update(kwargs)
        return object()

    class FakeRunner:
        def __init__(self, config: object) -> None:
            self.config = config

        async def stream(self):
            yield query.SessionComplete(reason="success")

        def approve(self, request_id: str, decision: Any) -> bool:
            return False

        def cancel_pending_approvals(self, message: str) -> None:
            return None

    monkeypatch.setattr(query, "QueryConfig", fake_config)
    monkeypatch.setattr(query, "QueryRunner", FakeRunner)
    session = ClawcodexSession(
        SessionSpec(
            cwd="/tmp",
            total_timeout_s=90.0,
            inactivity_timeout_s=45.0,
            stall_warn_s=5.0,
        )
    )
    try:
        await session.send("hello")
        _ = [event async for event in session.events()]
    finally:
        await session.close()

    assert captured["timeout_s"] == 90.0
    assert captured["stall_timeout_s"] == 45.0
    assert captured["stall_warn_s"] == 5.0


@pytest.mark.asyncio
async def test_approve_releases_current_query_runner(monkeypatch) -> None:
    from extensions.api import query
    from orchestratord_clawcodex.session import ClawcodexSession

    approvals: list[tuple[str, ApprovalDecision]] = []
    approval_resolved = None

    class FakeRunner:
        def __init__(self, config: object) -> None:
            nonlocal approval_resolved
            self.config = config
            import asyncio

            approval_resolved = asyncio.Event()

        async def stream(self):
            yield query.ApprovalRequestEvent(
                request_id="approval-1",
                call_id="tool-1",
                tool_name="Bash",
                arguments={"command": "echo ok"},
                message="run command?",
            )
            assert approval_resolved is not None
            await approval_resolved.wait()
            yield query.SessionComplete(reason="success")

        def approve(self, request_id: str, decision: ApprovalDecision) -> bool:
            approvals.append((request_id, decision))
            assert approval_resolved is not None
            approval_resolved.set()
            return True

        def cancel_pending_approvals(self, message: str) -> None:
            return None

    monkeypatch.setattr(query, "QueryRunner", FakeRunner)
    session = ClawcodexSession(SessionSpec(cwd="/tmp"))
    try:
        await session.send("hello")
        events = session.events()
        request = await anext(events)
        assert request.kind is EventKind.APPROVAL_REQUEST
        assert request.payload["call_id"] == "tool-1"
        await session.approve("approval-1", ApprovalDecision.ALLOW)
        remaining = [event async for event in events]
    finally:
        await session.close()

    assert approvals == [("approval-1", ApprovalDecision.ALLOW)]
    assert remaining[-1].kind is EventKind.SESSION_COMPLETE


@pytest.mark.asyncio
async def test_native_turn_counts_are_accumulated_across_sends(monkeypatch) -> None:
    from extensions.api import query
    from orchestratord_clawcodex.session import ClawcodexSession

    turn_counts = iter((3, 2))

    class FakeRunner:
        def __init__(self, config: object) -> None:
            self.config = config

        async def stream(self):
            yield query.TurnComplete(turn=next(turn_counts))
            yield query.SessionComplete(reason="success")

        def approve(self, request_id: str, decision: Any) -> bool:
            return False

        def cancel_pending_approvals(self, message: str) -> None:
            return None

    monkeypatch.setattr(query, "QueryRunner", FakeRunner)
    session = ClawcodexSession(SessionSpec(cwd="/tmp"))
    try:
        await session.send("first")
        first = [event async for event in session.events()]
        await session.send("second")
        second = [event async for event in session.events()]
    finally:
        await session.close()

    first_turn = next(
        event for event in first if event.kind is EventKind.TURN_COMPLETE
    )
    second_turn = next(
        event for event in second if event.kind is EventKind.TURN_COMPLETE
    )
    assert first_turn.payload == {"reason": "completed", "turn": 3, "turn_delta": 3}
    assert second_turn.payload == {"reason": "completed", "turn": 5, "turn_delta": 2}
