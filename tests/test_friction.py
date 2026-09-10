"""Tests for telemetry.friction (DESIGN_EXPERIENCE_LOOP.md §5)."""

from __future__ import annotations

import pytest

from orchestratord.telemetry.friction import (
    FrictionSignals,
    collect_signals,
    score,
)


@pytest.fixture
def telemetry_home(tmp_path, monkeypatch):
    monkeypatch.setenv("ORCHESTRATORD_HOME", str(tmp_path / "home"))
    from orchestratord.telemetry import storage

    yield storage


def _sig(**kw) -> FrictionSignals:
    return FrictionSignals(**kw)


def test_clean_session_scores_zero():
    assert score(_sig()) == 0


def test_each_signal_contributes_weight():
    assert score(_sig(retry_count=1)) == 15
    assert score(_sig(degradations=1)) == 8
    assert score(_sig(approvals=1)) == 5
    assert score(_sig(errors=1)) == 10
    assert score(_sig(errors=1, recoveries=1)) == 16


def test_retry_cap_applies():
    # 4+ retries are capped at 45 points (3 × 15).
    assert score(_sig(retry_count=4)) == 45
    assert score(_sig(retry_count=10)) == 45


def test_score_clamped_to_100():
    noisy = _sig(
        retry_count=5, degradations=5, approvals=5, errors=1, recoveries=1
    )
    assert score(noisy) == 100


def test_score_clamped_to_zero_for_negative_inputs():
    # Adversarial inputs must not escape the 0-100 contract at the low
    # end either (unreachable via collect_signals, but score is a pure
    # function with a documented range).
    bad = _sig(retry_count=-3, degradations=-2, errors=-5)
    assert score(bad) == 0


def test_duration_outlier_requires_baseline():
    sig = _sig(duration_s=1000.0)
    assert score(sig, None) == 0
    assert score(sig, {"p50": 10.0, "p95": 100.0}) == 10
    # Within 2×p95 — no outlier.
    assert score(_sig(duration_s=150.0), {"p50": 10.0, "p95": 100.0}) == 0
    # Degenerate zero p95 is ignored.
    assert score(sig, {"p50": 0.0, "p95": 0.0}) == 0


def test_collect_signals_aggregates_by_session(telemetry_home):
    from orchestratord.telemetry.recorder import (
        record_approval,
        record_degradation,
        record_error,
        record_retry_scheduled,
        record_session_end,
    )

    record_retry_scheduled(session_id="s1", attempt=1, delay_ms=100, reason="boom")
    record_retry_scheduled(session_id="s1", attempt=2, delay_ms=200, reason="boom2")
    record_degradation(session_id="s1", reason="streaming")
    record_approval(session_id="s1", tool="Bash", decision="deny")
    record_error(session_id="s1", error="x")
    record_session_end(session_id="s1", reason="success")
    # Noise from another session must not bleed in.
    record_degradation(session_id="other", reason="streaming")

    signals = collect_signals("s1", duration_s=42.0)
    assert signals.retry_count == 2
    assert signals.retry_reasons == ["boom", "boom2"]
    assert signals.degradations == 1
    assert signals.approvals == 1
    assert signals.errors == 1
    assert signals.recoveries == 1
    assert signals.duration_s == 42.0


def test_collect_signals_empty_store(telemetry_home):
    signals = collect_signals("")
    assert score(signals) == 0
