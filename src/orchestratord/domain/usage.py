"""Usage aggregation entity model (``docs/FEATURE_GAP_VS_MULTICA.md`` §5.2.4,
§6.1.1).

Token / cost usage is aggregated from ``SESSION_COMPLETE`` events' ``usage``
field (§5.2.4): one :class:`UsageRecord` is one session's contribution. The
router pre-aggregates those records into the ``usage_aggregates`` table at the
finest grouping granularity the dashboard needs — ``(workspace_id, agent_id,
issue_id, backend, day)`` — so every ``group_by`` dimension (§5.2.4's
workspace / agent / issue / backend, plus the daily line chart) can be served
by summing pre-aggregated rows on read.

Model invariants:

* token counts are non-negative integers
* ``cost_usd`` is non-negative — clawcodex / claude report ``total_cost_usd``;
  other backends fall back to a token estimator (``degradation.py``)
* ``recorded_at`` is timezone-aware (aggregation buckets by calendar day)
* ``group_by`` is whitelisted: workspace / agent / issue / backend / day
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from uuid import UUID

_GROUP_BYS = frozenset({"workspace", "agent", "issue", "backend", "day"})


def _normalize_recorded_at(value: datetime | None) -> datetime:
    """Return a tz-aware ``recorded_at``, matching the other entities."""
    if value is None:
        return datetime.now(UTC)
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


@dataclass
class UsageRecord:
    workspace_id: UUID
    tokens_in: int
    tokens_out: int
    cost_usd: float
    agent_id: UUID | None = None
    issue_id: UUID | None = None
    backend: str = ""
    recorded_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.tokens_in < 0 or self.tokens_out < 0:
            raise ValueError("token counts must be non-negative")
        if self.cost_usd < 0:
            raise ValueError("cost_usd must be non-negative")
        self.recorded_at = _normalize_recorded_at(self.recorded_at)

    @property
    def tokens_total(self) -> int:
        return self.tokens_in + self.tokens_out

    @property
    def day(self) -> date:
        return self.recorded_at.date()


def _group_key(item, group_by: str) -> str:
    """Return the bucket key for *item* under *group_by*.

    *item* exposes ``workspace_id`` / ``agent_id`` / ``issue_id`` / ``backend``
    and a ``day`` :class:`date` — satisfied by both the per-session
    :class:`UsageRecord` and the pre-aggregated ``usage_aggregates`` row.
    """
    if group_by == "workspace":
        return str(item.workspace_id)
    if group_by == "agent":
        return str(item.agent_id) if item.agent_id else "unassigned"
    if group_by == "issue":
        return str(item.issue_id) if item.issue_id else "unassigned"
    if group_by == "backend":
        return item.backend or "unknown"
    return item.day.isoformat()


def _aggregate(items, group_by: str, session_of) -> list[dict]:
    """Bucket *items* by *group_by*; *session_of* maps an item to its count."""
    if group_by not in _GROUP_BYS:
        raise ValueError(
            f"invalid group_by {group_by!r}; expected one of {sorted(_GROUP_BYS)}"
        )
    buckets: dict[str, dict] = {}
    order: list[str] = []
    for item in items:
        key = _group_key(item, group_by)
        if key not in buckets:
            buckets[key] = {
                "group": key,
                "tokens_in": 0,
                "tokens_out": 0,
                "tokens_total": 0,
                "cost_usd": 0.0,
                "sessions": 0,
            }
            order.append(key)
        bucket = buckets[key]
        bucket["tokens_in"] += item.tokens_in
        bucket["tokens_out"] += item.tokens_out
        bucket["tokens_total"] += item.tokens_in + item.tokens_out
        bucket["cost_usd"] += item.cost_usd
        bucket["sessions"] += session_of(item)
    return [buckets[k] for k in order]


def _totals(items, session_of) -> dict:
    """Aggregate *items* into a single totals row."""
    return {
        "tokens_in": sum(i.tokens_in for i in items),
        "tokens_out": sum(i.tokens_out for i in items),
        "tokens_total": sum(i.tokens_in + i.tokens_out for i in items),
        "cost_usd": sum(i.cost_usd for i in items),
        "sessions": sum(session_of(i) for i in items),
    }


def usage_totals(records: list[UsageRecord]) -> dict:
    """Aggregate a per-session record list into a single totals row."""
    return _totals(records, lambda r: 1)


def aggregate_usage(records: list[UsageRecord], group_by: str) -> list[dict]:
    """Group per-session *records* by *group_by*, returning one bucket per key."""
    return _aggregate(records, group_by, lambda r: 1)


def usage_totals_rows(rows) -> dict:
    """Aggregate pre-aggregated ``usage_aggregates`` rows into one totals row."""
    return _totals(rows, lambda r: r.sessions)


def aggregate_usage_rows(rows, group_by: str) -> list[dict]:
    """Group pre-aggregated ``usage_aggregates`` rows by *group_by*.

    Unlike :func:`aggregate_usage`, ``sessions`` is summed (a row already
    represents ``row.sessions`` sessions) rather than counted per item.
    """
    return _aggregate(rows, group_by, lambda r: r.sessions)


__all__ = [
    "UsageRecord",
    "aggregate_usage",
    "aggregate_usage_rows",
    "usage_totals",
    "usage_totals_rows",
]
