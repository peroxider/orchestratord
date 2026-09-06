"""Autopilot entity invariants (§7.3).

Autopilots are cron-like periodic tasks that launch a declarative workflow.
Invariants:

* ``enabled`` is a boolean flag defaulting to True.
* ``autopilot_runs`` record the schedule/start/finish lifecycle and link
  back to the executed workflow via ``run_id``.

Reference: docs/FEATURE_GAP_VS_MULTICA.md §7.3.
"""
from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4


def _autopilot(**overrides):
    from orchestratord.domain.autopilot import Autopilot  # type: ignore

    defaults = {
        "id": uuid4(),
        "workspace_id": uuid4(),
        "name": "nightly-triage",
        "cron": "0 0 * * *",
        "prompt": "triage new issues",
        "target_kind": "issue",
        "target_id": uuid4(),
    }
    defaults.update(overrides)
    return Autopilot(**defaults)


class TestAutopilotFields:
    """All schedule-definition fields are present."""

    def test_required_fields_present(self) -> None:
        a = _autopilot()
        for key in (
            "id", "workspace_id", "name", "cron", "prompt",
            "target_kind", "target_id", "enabled",
        ):
            assert getattr(a, key) is not None, f"missing {key!r}"

    def test_enabled_defaults_to_true(self) -> None:
        a = _autopilot()
        assert a.enabled is True

    def test_disabled_flag_preserved(self) -> None:
        a = _autopilot(enabled=False)
        assert a.enabled is False


class TestAutopilotRun:
    """A run records its lifecycle and links to the executed workflow."""

    def test_run_records_lifecycle(self) -> None:
        from orchestratord.domain.autopilot import AutopilotRun  # type: ignore

        aid = uuid4()
        run_id = uuid4()
        scheduled = datetime.now(UTC)
        r = AutopilotRun(
            autopilot_id=aid,
            scheduled_at=scheduled,
            run_id=run_id,
            status="completed",
        )
        assert r.autopilot_id == aid
        assert r.scheduled_at == scheduled
        assert r.run_id == run_id
        assert r.status == "completed"

    def test_start_finish_default_to_none(self) -> None:
        from orchestratord.domain.autopilot import AutopilotRun  # type: ignore

        r = AutopilotRun(
            autopilot_id=uuid4(),
            scheduled_at=datetime.now(UTC),
            run_id=uuid4(),
            status="scheduled",
        )
        assert r.started_at is None
        assert r.finished_at is None
