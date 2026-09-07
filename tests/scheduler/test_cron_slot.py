"""Cron slot math for AutopilotScheduler (§7.1) — pure unit tests."""

from __future__ import annotations

from datetime import UTC, datetime

from orchestratord.scheduler.autopilot import last_due_slot


class TestLastDueSlot:
    def test_every_five_minutes(self) -> None:
        now = datetime(2026, 9, 7, 10, 7, 0, tzinfo=UTC)
        slot = last_due_slot("*/5 * * * *", now)
        assert slot == datetime(2026, 9, 7, 10, 5, 0, tzinfo=UTC)

    def test_every_minute_between_boundaries(self) -> None:
        now = datetime(2026, 9, 7, 10, 7, 30, tzinfo=UTC)
        slot = last_due_slot("* * * * *", now)
        assert slot == datetime(2026, 9, 7, 10, 7, 0, tzinfo=UTC)

    def test_daily_cron_returns_today_slot(self) -> None:
        now = datetime(2026, 9, 7, 10, 7, 0, tzinfo=UTC)
        slot = last_due_slot("0 2 * * *", now)
        assert slot == datetime(2026, 9, 7, 2, 0, 0, tzinfo=UTC)

    def test_timezone_aware_output(self) -> None:
        now = datetime(2026, 9, 7, 10, 7, 0, tzinfo=UTC)
        assert last_due_slot("*/5 * * * *", now).tzinfo is not None
