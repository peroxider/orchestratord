"""Tests for the 5-level SessionSpec timeout classification (ADR-003).

DESIGN_graded_timeouts_and_resume.md §1.4 invariants:

  * sub-timeouts (handshake, first_turn, inactivity) must each not exceed
    ``total_timeout_s`` — the run watchdog must envelop every phase
  * ``idle_watchdog_timeout_s`` must not be smaller than
    ``total_timeout_s`` — a watchdog shorter than the run itself is
    meaningless
  * deprecated ``timeout_s`` / ``stall_timeout_s`` aliases are folded
    into ``total_timeout_s`` / ``inactivity_timeout_s`` respectively

These tests pin the validation contract; they do NOT exercise the
runtime enforcement (that lives in test_backend_runner.py).
"""

from __future__ import annotations

import pytest

from orchestratord.spi.backend import SessionSpec


class TestSessionSpecDefaults:
    """All optional fields default to None; construction without any
    timeout keyword must succeed."""

    def test_minimal_construction(self) -> None:
        spec = SessionSpec(cwd="/tmp")
        assert spec.cwd == "/tmp"
        assert spec.timeout_s is None
        assert spec.stall_timeout_s is None
        assert spec.stall_warn_s is None
        assert spec.total_timeout_s is None
        assert spec.handshake_timeout_s is None
        assert spec.first_turn_timeout_s is None
        assert spec.inactivity_timeout_s is None
        assert spec.idle_watchdog_timeout_s is None

    def test_explicit_5_fields_round_trip(self) -> None:
        spec = SessionSpec(
            cwd="/tmp",
            total_timeout_s=1800.0,
            handshake_timeout_s=30.0,
            first_turn_timeout_s=120.0,
            inactivity_timeout_s=300.0,
            idle_watchdog_timeout_s=1800.0,
        )
        assert spec.total_timeout_s == 1800.0
        assert spec.handshake_timeout_s == 30.0
        assert spec.first_turn_timeout_s == 120.0
        assert spec.inactivity_timeout_s == 300.0
        assert spec.idle_watchdog_timeout_s == 1800.0


class TestDeprecatedAliases:
    """``timeout_s`` and ``stall_timeout_s`` fold into the new fields
    when those fields are unset."""

    def test_timeout_s_folds_into_total(self) -> None:
        spec = SessionSpec(cwd="/tmp", timeout_s=600.0)
        assert spec.timeout_s == 600.0
        assert spec.total_timeout_s == 600.0

    def test_stall_timeout_s_folds_into_inactivity(self) -> None:
        spec = SessionSpec(cwd="/tmp", stall_timeout_s=120.0)
        assert spec.stall_timeout_s == 120.0
        assert spec.inactivity_timeout_s == 120.0

    def test_explicit_total_wins_over_deprecated(self) -> None:
        spec = SessionSpec(
            cwd="/tmp", timeout_s=600.0, total_timeout_s=900.0,
        )
        # Explicit value takes precedence over the deprecated alias.
        assert spec.total_timeout_s == 900.0

    def test_explicit_inactivity_wins_over_deprecated(self) -> None:
        spec = SessionSpec(
            cwd="/tmp",
            stall_timeout_s=120.0,
            inactivity_timeout_s=240.0,
        )
        assert spec.inactivity_timeout_s == 240.0


class TestSubTimeoutInvariants:
    """Each sub-timeout must not exceed total_timeout_s."""

    @pytest.mark.parametrize("field", [
        "handshake_timeout_s",
        "first_turn_timeout_s",
        "inactivity_timeout_s",
    ])
    def test_sub_timeout_exceeds_total_raises(self, field: str) -> None:
        kwargs: dict[str, object] = {
            "cwd": "/tmp",
            "total_timeout_s": 10.0,
            field: 20.0,
        }
        with pytest.raises(ValueError, match="sub-timeout"):
            SessionSpec(**kwargs)  # type: ignore[arg-type]

    def test_sub_timeout_equals_total_is_ok(self) -> None:
        # Boundary: equality is allowed (the watchdog still fires
        # at the right moment). Only strict-greater-than fails.
        SessionSpec(
            cwd="/tmp",
            total_timeout_s=10.0,
            handshake_timeout_s=10.0,
        )

    def test_sub_timeout_below_total_is_ok(self) -> None:
        SessionSpec(
            cwd="/tmp",
            total_timeout_s=1800.0,
            handshake_timeout_s=30.0,
            first_turn_timeout_s=120.0,
            inactivity_timeout_s=300.0,
        )


class TestIdleWatchdogInvariant:
    """``idle_watchdog_timeout_s`` must be >= ``total_timeout_s``."""

    def test_idle_smaller_than_total_raises(self) -> None:
        with pytest.raises(ValueError, match="idle_watchdog"):
            SessionSpec(
                cwd="/tmp",
                total_timeout_s=1800.0,
                idle_watchdog_timeout_s=600.0,
            )

    def test_idle_equal_to_total_is_ok(self) -> None:
        SessionSpec(
            cwd="/tmp",
            total_timeout_s=1800.0,
            idle_watchdog_timeout_s=1800.0,
        )

    def test_idle_greater_than_total_is_ok(self) -> None:
        SessionSpec(
            cwd="/tmp",
            total_timeout_s=1800.0,
            idle_watchdog_timeout_s=3600.0,
        )


class TestValidationSkippedWhenTotalAbsent:
    """The validation must only run when ``total_timeout_s`` is set."""

    def test_no_total_no_sub_timeouts_no_raise(self) -> None:
        # No invariants can be violated if total_timeout_s is None.
        SessionSpec(cwd="/tmp", handshake_timeout_s=10.0)

    def test_no_total_idle_watchdog_alone_no_raise(self) -> None:
        SessionSpec(
            cwd="/tmp", idle_watchdog_timeout_s=10.0,
        )


class TestBackwardCompatConstruction:
    """The pre-ADR-003 callers used only the deprecated aliases.
    They must continue to construct a valid spec without raising."""

    def test_legacy_construction(self) -> None:
        spec = SessionSpec(
            cwd="/tmp",
            timeout_s=1800.0,
            stall_timeout_s=300.0,
            stall_warn_s=30.0,
        )
        # Aliases fold into new fields.
        assert spec.total_timeout_s == 1800.0
        assert spec.inactivity_timeout_s == 300.0
        assert spec.stall_warn_s == 30.0