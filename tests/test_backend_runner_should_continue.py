"""Regression tests for ``BackendRunner._should_continue``.

``fetch_issue_states_by_ids`` returns ``dict[str, Issue]`` — normalized
``Issue`` dataclass objects, not plain dicts.  The legacy
``state.get("active", True)`` call raised ``AttributeError`` on every
poll, and the broad ``except Exception: return True`` swallowed it
silently — so the "stop once the issue is closed" safety net never
fired and the run kept burning tokens (and could open a PR) against a
closed issue.

These tests lock the fix:
  1. the active check reads the ``Issue.state`` attribute and compares it
     against the tracker's ``active_states`` (closed ⇒ stop);
  2. the failure path surfaces as a ``logger.warning`` instead of being
     silently converted into "continue".
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any

import pytest

from orchestratord.backend_runner import BackendRunner
from orchestratord.issue_registry.issue import Issue


def _runner() -> BackendRunner:
    """Build a bare runner — ``_should_continue`` touches no instance state."""
    return object.__new__(BackendRunner)


def _session(issue_id: str = "issue-1") -> SimpleNamespace:
    return SimpleNamespace(issue=Issue(id=issue_id))


class _Tracker:
    """Minimal tracker double: returns a fixed issue-state mapping."""

    def __init__(
        self,
        states: dict[str, Issue],
        active_states: list[str] | None = None,
    ) -> None:
        self._states = states
        self.active_states = active_states or ["open"]

    async def fetch_issue_states_by_ids(
        self, issue_ids: list[str]
    ) -> dict[str, Issue]:
        return {
            issue_id: self._states[issue_id]
            for issue_id in issue_ids
            if issue_id in self._states
        }


@pytest.mark.asyncio
async def test_should_continue_false_when_issue_closed() -> None:
    """A closed issue must stop the run — this was the silent no-op bug.

    The tracker returns ``dict[str, Issue]``; the snapshot's ``state``
    attribute is ``"closed"`` which is not in the tracker's
    ``active_states``.  ``_should_continue`` must return ``False`` so the
    event loop breaks instead of starting another turn.
    """
    tracker = _Tracker(
        states={"issue-1": Issue(id="issue-1", state="closed")},
        active_states=["open"],
    )
    assert await _runner()._should_continue(_session("issue-1"), tracker) is False


@pytest.mark.asyncio
async def test_should_continue_true_when_issue_active() -> None:
    """An issue still in an active state keeps the run going."""
    tracker = _Tracker(
        states={"issue-1": Issue(id="issue-1", state="in_progress")},
        active_states=["open", "in_progress"],
    )
    assert await _runner()._should_continue(_session("issue-1"), tracker) is True


@pytest.mark.asyncio
async def test_should_continue_true_when_issue_missing_from_fetch() -> None:
    """A fetch that omits the issue is not treated as "closed"."""
    tracker = _Tracker(states={}, active_states=["open"])
    assert await _runner()._should_continue(_session("issue-1"), tracker) is True


@pytest.mark.asyncio
async def test_should_continue_true_when_tracker_has_no_active_states() -> None:
    """Without an active-state vocabulary the legacy conservative default
    (continue) applies."""
    tracker = _Tracker(states={"issue-1": Issue(id="issue-1", state="closed")})
    tracker.active_states = []
    assert await _runner()._should_continue(_session("issue-1"), tracker) is True


@pytest.mark.asyncio
async def test_should_continue_logs_warning_on_tracker_failure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The failure path must not be silent: a tracker exception surfaces
    as a warning while still failing safe (continue)."""

    class _BrokenTracker:
        def __init__(self) -> None:
            self.active_states: list[str] = ["open"]

        async def fetch_issue_states_by_ids(self, issue_ids: list[str]) -> Any:
            raise RuntimeError("tracker exploded")

    runner = _runner()
    with caplog.at_level(logging.WARNING, logger="orchestratord.backend_runner"):
        assert await runner._should_continue(_session("issue-1"), _BrokenTracker()) is True

    warning_records = [
        r for r in caplog.records if r.levelno >= logging.WARNING
    ]
    assert warning_records, "tracker failure must produce a warning log record"
    assert any("should_continue" in r.getMessage() for r in warning_records)
