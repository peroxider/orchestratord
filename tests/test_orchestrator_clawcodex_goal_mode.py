"""Unit tests for goal-mode (Ralph-loop) integration in ClawcodexSession.

Per ADR-002 §7, these tests mock the GoalManager (which lives in the
clawcodex source tree, not in the orchestratord repo) and verify the
ClawcodexSession wiring: event emission, recursive continuation,
failure-mode handling, and workspace-change detection.
"""

from __future__ import annotations

import logging
import sys
from types import ModuleType
from unittest.mock import MagicMock

import pytest

from orchestratord.spi.backend import SessionSpec
from orchestratord.spi.events import EventKind


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_spec(**kwargs) -> SessionSpec:
    defaults = {"cwd": "/work/test"}
    defaults.update(kwargs)
    return SessionSpec(**defaults)


def _collect_events(session) -> list:
    """Drain the event queue synchronously (non-blocking)."""
    events = []
    while not session._event_queue.empty():
        event = session._event_queue.get_nowait()
        if event is not None:
            events.append(event)
    return events


def _make_mock_goal_manager(state_overrides=None):
    """Build a mock GoalManager with realistic state."""
    mgr = MagicMock()
    mgr.state.max_turns = 20
    mgr.state.turns_used = 0
    mgr.state.spent_tokens = 0
    if state_overrides:
        for k, v in state_overrides.items():
            setattr(mgr.state, k, v)
    mgr.is_active.return_value = True
    mgr.state.to_dict.return_value = {"max_turns": 20, "turns_used": 0}
    return mgr


def _inject_mock_goals_module(goal_mgr=None, build_judge=None):
    """Inject a mock ``src.goals`` module into sys.modules.

    Returns a cleanup function that removes the mock module.
    """
    mock_module = ModuleType("src.goals")
    if goal_mgr is not None:
        mock_module.GoalManager = goal_mgr
    if build_judge is not None:
        mock_module.build_judge_callable = build_judge
    sys.modules["src.goals"] = mock_module

    def cleanup():
        sys.modules.pop("src.goals", None)

    return cleanup


# ---------------------------------------------------------------------------
# Test 1: Judge returns done → GOAL_DONE
# ---------------------------------------------------------------------------


def test_goal_judge_returns_done():
    """Mock evaluate_after_turn returning done → GOAL_DONE emitted."""
    mgr = _make_mock_goal_manager()
    mgr.evaluate_after_turn.return_value = {
        "verdict": "done",
        "should_continue": False,
        "status": "done",
        "reason": "goal achieved",
    }

    cleanup = _inject_mock_goals_module(
        goal_mgr=MagicMock(return_value=mgr),
        build_judge=MagicMock(),
    )
    try:
        from orchestratord_clawcodex.session import ClawcodexSession
        session = ClawcodexSession(
            _make_spec(goal_condition="Print 42 and stop")
        )
    finally:
        cleanup()

    assert session._goal_mgr is not None
    mgr.set.assert_called_once()

    import asyncio
    # Mock _run_turn to avoid QueryRunner import — just call _evaluate_goal
    # again to simulate the recursive continuation loop.
    original_run_turn = session._run_turn
    async def _mock_run_turn(content):
        session._evaluate_goal = original_evaluate
        await session._evaluate_goal()
    session._run_turn = _mock_run_turn
    original_evaluate = session._evaluate_goal
    asyncio.run(session._evaluate_goal())

    events = _collect_events(session)
    kinds = [e.kind for e in events]

    assert EventKind.GOAL_SET in kinds, f"Expected GOAL_SET, got {kinds}"
    assert EventKind.GOAL_STATUS in kinds
    assert EventKind.GOAL_DONE in kinds
    done_events = [e for e in events if e.kind == EventKind.GOAL_DONE]
    assert len(done_events) == 1
    assert done_events[0].payload["reason"] == "goal achieved"


# ---------------------------------------------------------------------------
# Test 2: Judge returns continue → GOAL_CONTINUE + recursive _run_turn
# ---------------------------------------------------------------------------


def test_goal_judge_returns_continue():
    """Mock evaluate_after_turn returning continue → GOAL_CONTINUE + recursive call."""
    call_count = [0]

    def _evaluate_side_effect(evidence, tokens_now, cost_now_usd):
        call_count[0] += 1
        if call_count[0] == 1:
            return {
                "verdict": "continue",
                "should_continue": True,
                "continuation_prompt": "CONTINUE: keep working",
                "status": "in_progress",
                "reason": "not done yet",
            }
        return {
            "verdict": "done",
            "should_continue": False,
            "status": "done",
            "reason": "done after continuation",
        }

    mgr = _make_mock_goal_manager()
    mgr.evaluate_after_turn.side_effect = _evaluate_side_effect

    cleanup = _inject_mock_goals_module(
        goal_mgr=MagicMock(return_value=mgr),
        build_judge=MagicMock(),
    )
    try:
        from orchestratord_clawcodex.session import ClawcodexSession
        session = ClawcodexSession(
            _make_spec(goal_condition="Keep going until done")
        )
    finally:
        cleanup()

    import asyncio
    # Mock _run_turn to avoid QueryRunner import — just call _evaluate_goal
    # again to simulate the recursive continuation loop.
    async def _mock_run_turn(content):
        await session._evaluate_goal()
    original_run_turn = session._run_turn
    session._run_turn = _mock_run_turn
    try:
        asyncio.run(session._evaluate_goal())
    finally:
        session._run_turn = original_run_turn

    events = _collect_events(session)
    kinds = [e.kind for e in events]

    assert EventKind.GOAL_CONTINUE in kinds
    assert EventKind.GOAL_DONE in kinds
    continue_events = [e for e in events if e.kind == EventKind.GOAL_CONTINUE]
    assert len(continue_events) == 1
    assert "keep working" in continue_events[0].payload["continuation_prompt"]


# ---------------------------------------------------------------------------
# Test 3: Judge returns timeout → GOAL_PAUSED, no more turns
# ---------------------------------------------------------------------------


def test_goal_judge_returns_timeout():
    """Mock evaluate_after_turn returning timeout → GOAL_PAUSED, no recursion."""
    mgr = _make_mock_goal_manager()
    mgr.evaluate_after_turn.return_value = {
        "verdict": "timeout",
        "should_continue": False,
        "status": "paused",
        "reason": "judge timed out",
    }

    cleanup = _inject_mock_goals_module(
        goal_mgr=MagicMock(return_value=mgr),
        build_judge=MagicMock(),
    )
    try:
        from orchestratord_clawcodex.session import ClawcodexSession
        session = ClawcodexSession(
            _make_spec(goal_condition="Do something")
        )
    finally:
        cleanup()

    import asyncio
    asyncio.run(session._evaluate_goal())

    events = _collect_events(session)
    kinds = [e.kind for e in events]

    assert EventKind.GOAL_PAUSED in kinds
    assert EventKind.GOAL_CONTINUE not in kinds
    assert EventKind.GOAL_DONE not in kinds
    paused = [e for e in events if e.kind == EventKind.GOAL_PAUSED]
    assert len(paused) == 1
    assert "resume_hint" in paused[0].payload


# ---------------------------------------------------------------------------
# Test 4: Judge parse-fail → auto-paused
# ---------------------------------------------------------------------------


def test_goal_parse_fail_triggers_pause():
    """Mock evaluate_after_turn returning paused verdict."""
    mgr = _make_mock_goal_manager()
    mgr.evaluate_after_turn.return_value = {
        "verdict": "paused",
        "should_continue": False,
        "status": "paused",
        "reason": "judge-parse-failures",
    }

    cleanup = _inject_mock_goals_module(
        goal_mgr=MagicMock(return_value=mgr),
        build_judge=MagicMock(),
    )
    try:
        from orchestratord_clawcodex.session import ClawcodexSession
        session = ClawcodexSession(
            _make_spec(goal_condition="Ambiguous task")
        )
    finally:
        cleanup()

    import asyncio
    asyncio.run(session._evaluate_goal())

    events = _collect_events(session)
    kinds = [e.kind for e in events]
    assert EventKind.GOAL_PAUSED in kinds


# ---------------------------------------------------------------------------
# Test 5: goal_max_turns passed through to GoalManager
# ---------------------------------------------------------------------------


def test_goal_max_turns_passed_to_manager():
    """SessionSpec(goal_condition=..., goal_max_turns=5) → GoalManager(default_max_turns=5)."""
    mgr = _make_mock_goal_manager()
    mock_goal_mgr_cls = MagicMock(return_value=mgr)

    cleanup = _inject_mock_goals_module(
        goal_mgr=mock_goal_mgr_cls,
        build_judge=MagicMock(),
    )
    try:
        from orchestratord_clawcodex.session import ClawcodexSession
        ClawcodexSession(_make_spec(
            goal_condition="Do it",
            goal_max_turns=5,
        ))
    finally:
        cleanup()

    mock_goal_mgr_cls.assert_called_once()
    _, kwargs = mock_goal_mgr_cls.call_args
    assert kwargs["default_max_turns"] == 5


# ---------------------------------------------------------------------------
# Test 6: No goal_condition → _goal_mgr is None, zero goal events
# ---------------------------------------------------------------------------


def test_no_goal_condition_no_goal_mgr():
    """SessionSpec(goal_condition=None) → _goal_mgr is None, zero goal events."""
    from orchestratord_clawcodex.session import ClawcodexSession

    session = ClawcodexSession(_make_spec(goal_condition=None))
    assert session._goal_mgr is None
    assert session._goal_workspace is None

    import asyncio
    asyncio.run(session._evaluate_goal())

    events = _collect_events(session)
    goal_events = [e for e in events if e.kind.value.startswith("goal_")]
    assert len(goal_events) == 0


# ---------------------------------------------------------------------------
# Test 7: ImportError on GoalManager → graceful degradation
# ---------------------------------------------------------------------------


def test_goal_set_gate_refused():
    """When GoalManager import fails, _goal_mgr stays None."""
    # Inject a mock module that raises on GoalManager access
    mock_module = ModuleType("src.goals")
    mock_module.build_judge_callable = MagicMock()
    # GoalManager is not set — accessing it will raise AttributeError
    sys.modules["src.goals"] = mock_module
    try:
        from orchestratord_clawcodex.session import ClawcodexSession
        session = ClawcodexSession(
            _make_spec(goal_condition="Do something")
        )
    finally:
        sys.modules.pop("src.goals", None)

    assert session._goal_mgr is None

    import asyncio
    asyncio.run(session._evaluate_goal())

    events = _collect_events(session)
    goal_events = [e for e in events if e.kind.value.startswith("goal_")]
    assert len(goal_events) == 0


# ---------------------------------------------------------------------------
# Test 8: Workspace-change clear → GOAL_CLEARED + WARNING log
# ---------------------------------------------------------------------------


def test_workspace_change_clears_goal(caplog):
    """Session A cwd=/work/a + goal, then cwd changes → GOAL_CLEARED + WARNING."""
    mgr = _make_mock_goal_manager()

    cleanup = _inject_mock_goals_module(
        goal_mgr=MagicMock(return_value=mgr),
        build_judge=MagicMock(),
    )
    try:
        from orchestratord_clawcodex.session import ClawcodexSession
        session = ClawcodexSession(
            _make_spec(cwd="/work/a", goal_condition="Finish the task")
        )
    finally:
        cleanup()

    assert session._goal_workspace == "/work/a"
    assert session._goal_mgr is not None

    session._spec = _make_spec(cwd="/work/b", goal_condition="Finish the task")

    with caplog.at_level(logging.WARNING):
        changed = session._check_workspace_change()

    assert changed is True
    assert session._goal_mgr is None
    assert session._goal_workspace is None

    events = _collect_events(session)
    cleared = [e for e in events if e.kind == EventKind.GOAL_CLEARED]
    assert len(cleared) == 1
    assert cleared[0].payload["reason"] == "workspace-changed"
    assert cleared[0].payload["from_workspace"] == "/work/a"
    assert cleared[0].payload["to_workspace"] == "/work/b"

    assert any(
        "workspace changed" in record.message
        and "/work/a" in record.message
        and "/work/b" in record.message
        for record in caplog.records
    )
