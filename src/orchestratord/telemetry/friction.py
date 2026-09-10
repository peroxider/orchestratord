"""Friction scoring for finished sessions (DESIGN_EXPERIENCE_LOOP.md §5).

Signals come exclusively from existing telemetry events (retry /
degradation / approval / error / session_end) plus the session's own
duration — no new instrumentation beyond the three ``record_*`` probes.
``score`` is a pure function over :class:`FrictionSignals` so tests can
construct signals directly; ``collect_signals`` is the only component
that touches the JSONL store.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from .storage import local_days, read_events

# Weights (first-cut calibration; revisit after Phase C data lands).
WEIGHTS: dict[str, int] = {
    "retry": 15,
    "degradation": 8,
    "approval": 5,
    "error": 10,
    "recovery": 6,
    "duration_outlier": 10,
}
# Per-signal caps keep one noisy category from dominating the score.
CAPS: dict[str, int] = {
    "retry": 45,
    "degradation": 24,
    "approval": 15,
    "error": 30,
    "recovery": 12,
}

_DAYS_BACK = 14


@dataclass
class FrictionSignals:
    retry_count: int = 0
    retry_reasons: list[str] = field(default_factory=list)
    degradations: int = 0
    approvals: int = 0
    errors: int = 0
    recoveries: int = 0
    duration_s: float = 0.0


def _percentile(sorted_values: list[float], q: float) -> float | None:
    """Nearest-rank percentile of an already-sorted list (0 <= q <= 1)."""
    if not sorted_values:
        return None
    idx = min(len(sorted_values) - 1, max(0, round(q * (len(sorted_values) - 1))))
    return sorted_values[idx]


def score(
    signals: FrictionSignals,
    duration_baseline: dict[str, float] | None = None,
) -> int:
    """Weighted 0-100 friction score.

    ``duration_baseline`` carries ``p50``/``p95`` duration seconds from
    prior sessions of the same workspace; a duration beyond 2×p95 adds
    the ``duration_outlier`` weight. Without a baseline the outlier
    check is skipped (degenerate cold-start).
    """
    points = 0
    points += min(signals.retry_count, 3) * WEIGHTS["retry"]
    points += min(signals.degradations * WEIGHTS["degradation"], CAPS["degradation"])
    points += min(signals.approvals * WEIGHTS["approval"], CAPS["approval"])
    points += min(signals.errors * WEIGHTS["error"], CAPS["error"])
    points += min(signals.recoveries * WEIGHTS["recovery"], CAPS["recovery"])
    if duration_baseline:
        p95 = duration_baseline.get("p95")
        if p95 and p95 > 0 and signals.duration_s > 2 * p95:
            points += WEIGHTS["duration_outlier"]
    return max(0, min(100, points))


def collect_signals(
    session_id: str,
    *,
    days_back: int = _DAYS_BACK,
    duration_s: float = 0.0,
) -> FrictionSignals:
    """Aggregate this session's friction-relevant events from the JSONL store."""
    signals = FrictionSignals(duration_s=duration_s)
    if not session_id:
        return signals
    days = local_days()
    if not days:
        return signals
    cutoff = time.strftime("%Y-%m-%d", time.gmtime(time.time() - days_back * 86400))
    saw_error = False
    saw_success_end = False
    for day in days:
        if day < cutoff:
            continue
        for event in read_events(day):
            if event.get("session_id") != session_id:
                continue
            etype = event.get("type", "")
            payload = event.get("payload") or {}
            if etype == "retry":
                signals.retry_count += 1
                reason = payload.get("reason") or payload.get("error") or ""
                if reason:
                    signals.retry_reasons.append(str(reason))
            elif etype == "degradation":
                signals.degradations += 1
            elif etype == "approval":
                signals.approvals += 1
            elif etype == "error":
                saw_error = True
            elif etype == "session_end":
                if payload.get("reason") in ("success", "turn_complete"):
                    saw_success_end = True
    signals.errors = 1 if saw_error else 0
    signals.recoveries = 1 if (saw_error and saw_success_end) else 0
    return signals


__all__ = ["FrictionSignals", "collect_signals", "score"]
