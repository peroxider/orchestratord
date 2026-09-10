"""Regression tests for ReviewFeedbackService poll_interval_ms throttling.

Verifies that ``collect_followups`` respects the configured
``poll_interval_ms`` so the daemon does NOT poll all PR feedback
on every main loop cycle — only when the interval has elapsed.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from orchestratord.review_feedback import ReviewFeedbackService


def _make_config(**overrides) -> SimpleNamespace:
    """Create a minimal ReviewFeedbackConfig-like namespace."""
    defaults = {
        "enabled": True,
        "poll_interval_ms": 60_000,
        "mode": "auto",
        "max_feedback_items_per_run": 20,
        "include_ci_failures": True,
        "reply_to_comments": True,
        "ignore_authors": [],
        "ignored_comment_commands": [],
        "ignored_feedback_sources": [],
        "ignored_body_patterns": [],
        "bot_login": None,
        "max_log_chars_per_check": 12_000,
        "max_followup_attempts_per_pr": 5,
        "pending_feedback_timeout_seconds": 600,
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _make_registry_with_pr(issue_id: str = "1") -> MagicMock:
    """Mock registry with one record having a PR number."""
    record = MagicMock()
    record.issue_id = issue_id
    record.issue_identifier = f"ISSUE-{issue_id}"
    record.branch_name = f"fix/issue-{issue_id}"
    record.pr_number = issue_id
    record.pr_url = f"https://example.test/pr/{issue_id}"
    record.processed_feedback_ids = []
    record.pending_feedback_ids = []

    registry = MagicMock()
    registry.iter_records_with_pr.return_value = [record]
    registry.clear_stale_pending.return_value = 0
    return registry


def _make_tracker() -> MagicMock:
    """Mock tracker that supports PullRequestFeedbackCapability."""
    tracker = MagicMock()
    # Async methods that the service awaits.
    tracker.get_authenticated_user = AsyncMock(return_value="test-bot")
    tracker.fetch_pull_request_feedback = AsyncMock(return_value=[])
    return tracker


# ---------------------------------------------------------------------------
# Red-Green regression tests for poll_interval_ms throttling
# ---------------------------------------------------------------------------


async def test_collect_throttled_when_interval_not_elapsed() -> None:
    """Second immediate call returns empty and does NOT invoke tracker.

    Regression: without poll_interval_ms checking, every main-loop
    cycle would trigger full PR-feedback fetch.  After the fix, a
    second call within the interval must short-circuit.
    """
    tracker = _make_tracker()
    registry = _make_registry_with_pr()
    config = _make_config(poll_interval_ms=60_000)

    service = ReviewFeedbackService(
        tracker=tracker,
        registry=registry,
        config=config,
    )

    # First call: always proceeds because _last_collect_monotonic starts
    # at 0.0 and time.monotonic() - 0.0 is always > any reasonable interval.
    await service.collect_followups(1)
    # Tracker should have been consulted.
    tracker.fetch_pull_request_feedback.assert_called_once()

    # Second call: interval (60s) has NOT elapsed → must be throttled.
    result2 = await service.collect_followups(1)
    assert result2 == [], (
        "collect_followups should return [] when poll_interval_ms "
        "has not elapsed; got non-empty result"
    )
    # Tracker call count must NOT have increased.
    assert (
        tracker.fetch_pull_request_feedback.call_count == 1
    ), "Tracker should NOT be called again within poll_interval_ms"


async def test_collect_allowed_after_interval_elapsed() -> None:
    """After poll_interval_ms elapses, collection proceeds normally."""
    tracker = _make_tracker()
    registry = _make_registry_with_pr()
    # Very short interval so we can wait for it in a unit test.
    config = _make_config(poll_interval_ms=1)  # 1 ms

    service = ReviewFeedbackService(
        tracker=tracker,
        registry=registry,
        config=config,
    )

    # First call: always allowed.
    await service.collect_followups(1)
    assert tracker.fetch_pull_request_feedback.call_count == 1

    # Wait 20ms to comfortably exceed the 1ms interval.
    await asyncio.sleep(0.02)

    # Second call: now the 1ms interval has elapsed.
    await service.collect_followups(1)
    assert tracker.fetch_pull_request_feedback.call_count == 2, (
        "collect_followups should proceed when poll_interval has elapsed"
    )


async def test_collect_not_throttled_when_poll_interval_disabled() -> None:
    """poll_interval_ms=0 disables throttling: every call proceeds.

    Boundary path: a 0 interval means "throttling disabled", so two
    back-to-back calls must both consult the tracker (no short-circuit).
    """
    tracker = _make_tracker()
    registry = _make_registry_with_pr()
    config = _make_config(poll_interval_ms=0)  # 0 = throttling disabled

    service = ReviewFeedbackService(
        tracker=tracker,
        registry=registry,
        config=config,
    )

    # Both immediate calls must proceed without throttling.
    await service.collect_followups(1)
    await service.collect_followups(1)
    assert tracker.fetch_pull_request_feedback.call_count == 2, (
        "poll_interval_ms=0 disables throttling; every call should "
        "consult the tracker"
    )


# ---------------------------------------------------------------------------
# Existing guard-clause behavior (no regression)
# ---------------------------------------------------------------------------


async def test_disabled_returns_empty() -> None:
    """When config.enabled is False, collect_followups returns []."""
    config = _make_config(enabled=False)
    service = ReviewFeedbackService(
        tracker=_make_tracker(),
        registry=_make_registry_with_pr(),
        config=config,
    )
    result = await service.collect_followups(5)
    assert result == []
    assert service.tracker.fetch_pull_request_feedback.call_count == 0


async def test_no_available_slots_returns_empty() -> None:
    """When available_slots <= 0, collect_followups returns []."""
    config = _make_config()
    service = ReviewFeedbackService(
        tracker=_make_tracker(),
        registry=_make_registry_with_pr(),
        config=config,
    )
    result = await service.collect_followups(0)
    assert result == []
    assert service.tracker.fetch_pull_request_feedback.call_count == 0