"""DispatchPolicy — 运行时状态与重试队列策略（DESIGN §5 机制职责表第 2 行）。

从 orchestrator.py 迁入的纯机制段：

- ``OrchestratorState``：轮询节奏/并发上限/运行中登记/重试队列的运行时状态。
- 重试纯策略：非重试终态判定、指数退避计算、队列就绪切分、requeue 决策。

registry 持久化、tracker 同步、IM 推送等业务副作用留在应用侧
（DESIGN §8 数据归属：issues/registry 归 issue_pr 应用）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..session_state import AgentSession, RetryItem

#: Operator-action end reasons — terminal states the auto-retry loop
#: must not revive (stop → retry → stop burning API calls until max
#: attempts). The operator can re-run the issue explicitly.
NON_RETRYABLE_END_REASONS = frozenset({
    "operator_stop",
    "operator_takeover",
    "operator_stopped",
})

#: Base delay for the failure retry exponential backoff curve (ms).
FAILURE_RETRY_BASE_MS = 10_000


@dataclass
class OrchestratorState:
    """Runtime state for the orchestrator polling loop."""

    poll_interval_ms: int = 30_000
    max_concurrent_agents: int = 10
    next_poll_due_at_ms: float | None = None
    poll_check_in_progress: bool = False
    running: dict[str, AgentSession] = field(default_factory=dict)
    completed: set[str] = field(default_factory=set)
    failed: set[str] = field(default_factory=set)
    pending_review: set[str] = field(default_factory=set)  # awaiting human review
    claimed: set[str] = field(default_factory=set)
    retry_queue: list[RetryItem] = field(default_factory=list)
    retry_attempts: dict[str, int] = field(default_factory=dict)
    # Throttle marker for the optional PR conflict scan. Wall-clock
    # seconds (not ms) of the last scan — compared against
    # ``time.monotonic()`` so a backwards clock jump is benign.
    pr_conflict_scan_last_run: float = 0.0
    pr_merge_close_last_run: float = 0.0
    codex_totals: dict[str, int] = field(
        default_factory=lambda: {
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
            "seconds_running": 0,
        }
    )


def compute_retry_delay(attempt: int, base_ms: int, max_ms: int) -> int:
    """Exponential backoff capped at ``max_ms`` (DESIGN §4.2 RETRY 策略)."""
    return min(base_ms * (1 << (attempt - 1)), max_ms)


def split_ready_retries(
    retry_queue: list[RetryItem], now: float
) -> tuple[list[RetryItem], list[RetryItem]]:
    """Split the retry queue into (ready, not_ready) at wall-clock ``now``.

    The caller rewrites the live queue with ``not_ready`` — the split
    never aliases the input list.
    """
    ready: list[RetryItem] = []
    not_ready: list[RetryItem] = []
    for retry in retry_queue:
        if now >= retry.scheduled_at + retry.delay_seconds:
            ready.append(retry)
        else:
            not_ready.append(retry)
    return ready, not_ready


def requeue_retry_item(
    retry: Any,
    now: float,
    *,
    requeue_limit: int,
    max_backoff_ms: float,
) -> bool:
    """Re-queue a retry with a doubled, capped delay (pure policy part).

    Mutates ``retry`` (requeue_count/delay_seconds/scheduled_at) but
    performs no persistence — the caller owns registry side effects and
    drops the item (clearing its persisted plan) when this returns
    ``False``.
    """
    retry.requeue_count = getattr(retry, "requeue_count", 0) + 1
    if retry.requeue_count > requeue_limit:
        return False
    retry.delay_seconds = min(retry.delay_seconds * 2, max_backoff_ms / 1000.0)
    retry.scheduled_at = now
    return True
