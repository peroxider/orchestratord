"""Usage aggregation invariants (§5.2.4, §6.1.1).

``UsageRecord`` is one session's token / cost contribution; ``aggregate_usage``
buckets records by a whitelisted dimension. Invariants:

* token counts and ``cost_usd`` are non-negative.
* ``recorded_at`` is timezone-aware.
* ``group_by`` ∈ {workspace, agent, issue, backend, day}; unknown values raise.

Reference: docs/FEATURE_GAP_VS_MULTICA.md §5.2.4, §6.1.1.
"""
from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from orchestratord.domain.usage import UsageRecord, aggregate_usage, usage_totals


def _record(**overrides) -> UsageRecord:
    defaults = {
        "workspace_id": uuid4(),
        "tokens_in": 10,
        "tokens_out": 5,
        "cost_usd": 0.25,
    }
    defaults.update(overrides)
    return UsageRecord(**defaults)


class TestUsageRecordFields:
    def test_required_fields_present(self) -> None:
        r = _record()
        assert r.workspace_id is not None
        assert r.tokens_in == 10
        assert r.tokens_out == 5
        assert r.cost_usd == 0.25

    def test_tokens_total(self) -> None:
        assert _record(tokens_in=30, tokens_out=20).tokens_total == 50

    def test_defaults(self) -> None:
        r = _record()
        assert r.agent_id is None
        assert r.issue_id is None
        assert r.backend == ""

    def test_recorded_at_is_timezone_aware(self) -> None:
        assert _record().recorded_at.tzinfo is not None

    def test_naive_recorded_at_normalized(self) -> None:
        naive = datetime.fromisoformat("2026-01-01T00:00:00")
        assert _record(recorded_at=naive).recorded_at.tzinfo is UTC


class TestUsageRecordValidation:
    def test_negative_tokens_in_rejected(self) -> None:
        with pytest.raises(ValueError, match="non-negative"):
            _record(tokens_in=-1)

    def test_negative_tokens_out_rejected(self) -> None:
        with pytest.raises(ValueError, match="non-negative"):
            _record(tokens_out=-1)

    def test_negative_cost_rejected(self) -> None:
        with pytest.raises(ValueError, match="non-negative"):
            _record(cost_usd=-0.5)


class TestAggregateUsage:
    def test_group_by_backend_sums(self) -> None:
        records = [
            _record(backend="codex", tokens_in=10, tokens_out=5, cost_usd=0.25),
            _record(backend="codex", tokens_in=20, tokens_out=5, cost_usd=0.5),
            _record(backend="claude", tokens_in=5, tokens_out=5, cost_usd=0.1),
        ]
        groups = aggregate_usage(records, "backend")
        by_key = {g["group"]: g for g in groups}
        assert by_key["codex"]["tokens_in"] == 30
        assert by_key["codex"]["tokens_out"] == 10
        assert by_key["codex"]["tokens_total"] == 40
        assert by_key["codex"]["cost_usd"] == pytest.approx(0.75)
        assert by_key["codex"]["sessions"] == 2
        assert by_key["claude"]["sessions"] == 1

    def test_group_by_day_buckets_by_date(self) -> None:
        day1 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
        day2 = datetime(2026, 1, 2, 12, 0, 0, tzinfo=UTC)
        records = [
            _record(recorded_at=day1),
            _record(recorded_at=day1),
            _record(recorded_at=day2),
        ]
        groups = aggregate_usage(records, "day")
        assert len(groups) == 2
        assert groups[0]["group"] == "2026-01-01"
        assert groups[0]["sessions"] == 2
        assert groups[1]["group"] == "2026-01-02"
        assert groups[1]["sessions"] == 1

    def test_unassigned_groups(self) -> None:
        groups = aggregate_usage([_record(agent_id=None)], "agent")
        assert groups[0]["group"] == "unassigned"

    def test_invalid_group_by_rejected(self) -> None:
        with pytest.raises(ValueError, match="group_by"):
            aggregate_usage([_record()], "month")


class TestUsageTotals:
    def test_totals_sum(self) -> None:
        records = [
            _record(tokens_in=10, tokens_out=5, cost_usd=0.25),
            _record(tokens_in=20, tokens_out=5, cost_usd=0.5),
        ]
        totals = usage_totals(records)
        assert totals["tokens_in"] == 30
        assert totals["tokens_out"] == 10
        assert totals["tokens_total"] == 40
        assert totals["cost_usd"] == pytest.approx(0.75)
        assert totals["sessions"] == 2
