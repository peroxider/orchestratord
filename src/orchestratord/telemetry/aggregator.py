"""Telemetry aggregator — daily run summary from the local event store.

Reads the day's events (``~/.orchestratord/telemetry/events/<date>.jsonl``)
via :func:`orchestratord.telemetry.storage.read_events` and reduces them
into a per-day summary. Beyond raw counts and usage the summary carries
the enriched dimensions recorded by the backend runner:

- session duration distribution (avg/p50/p95/max) over agent-run
  ``session_end`` events — agent runs are identified by the ``turn_count``
  key in the payload, which the orchestrator daemon's own session_end
  never carries;
- e2e breakdown per session (queue wait, first-event / first-turn
  latency, pause and 429-backoff totals);
- per-backend tokens/cost/sessions/duration rollup;
- failure reasons (session_end ``end_reason``) and backend error reasons;
- per-tool call/failure/duration aggregation from ``tool_summary``.
"""

from __future__ import annotations

import time
from collections import Counter
from typing import Any

from .storage import read_events


def _day_key() -> str:
    return time.strftime("%Y-%m-%d")


# session_end reasons that mean a human stepped into the loop: manual
# stop / takeover from the CLI, or a cancellation (人工取消或系统取消).
# Everything else — backend errors, watchdog timeouts, verification
# blocks followed by automatic retry — closes the loop unattended.
_HUMAN_END_REASONS = frozenset(
    {"operator_stop", "operator_stopped", "operator_takeover", "cancelled"}
)


def _percentile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, round(q * (len(ordered) - 1))))
    return ordered[idx]


def _duration_stats(values: list[float]) -> dict[str, Any]:
    if not values:
        return {
            "count": 0,
            "total_s": 0.0,
            "avg_s": 0.0,
            "p25_s": 0.0,
            "p50_s": 0.0,
            "p75_s": 0.0,
            "p95_s": 0.0,
            "max_s": 0.0,
        }
    total = sum(values)
    return {
        "count": len(values),
        "total_s": total,
        "avg_s": total / len(values),
        "p25_s": _percentile(values, 0.25),
        "p50_s": _percentile(values, 0.50),
        "p75_s": _percentile(values, 0.75),
        "p95_s": _percentile(values, 0.95),
        "max_s": max(values),
    }


def _num(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) else None


def aggregate_day(day: str | None = None) -> dict[str, Any]:
    """Aggregate one day of events into a summary dict.

    Base keys: date, events, sessions, commands, errors, usage_events,
    sessions_succeeded, sessions_failed, total_cost_usd, tokens
    (input/output summed across usage events when present), by_issue.
    Enriched keys: session_duration / session_e2e / turn_duration
    distributions, latency (queue/first-event/first-turn averages),
    paused_s / backoff_429_s totals, turns, by_backend, by_model,
    end_reasons_failed, errors_by_reason, tools. Cross-cutting:
    unattended (closed-loop rates incl. settled-issue adjusted rate),
    closed_loop_time / cost / retry (闭环效率), intervention (干预前
    征兆), concurrency / hourly (时间维度).
    """
    day = day or _day_key()
    events = read_events(day)

    summary: dict[str, Any] = {
        "date": day,
        "events": len(events),
        "sessions": 0,
        "sessions_ended": 0,
        "commands": 0,
        "errors": 0,
        "usage_events": 0,
        "sessions_succeeded": 0,
        "sessions_failed": 0,
        "total_cost_usd": 0.0,
        "tokens_input": 0,
        "tokens_output": 0,
        "by_issue": Counter(),
        "session_duration": _duration_stats([]),
        "session_e2e": _duration_stats([]),
        "latency": {
            "queue_wait_avg_s": 0.0,
            "first_event_avg_s": 0.0,
            "first_turn_avg_s": 0.0,
        },
        "paused_s": 0.0,
        "backoff_429_s": 0.0,
        "turns": {
            "total": 0,
            "turn_events": 0,
            "total_s": 0.0,
            "avg_s": 0.0,
        },
        "by_backend": {},
        "by_model": {},
        "end_reasons_failed": Counter(),
        "errors_by_reason": Counter(),
        "tools": {},
        "verification": {
            "attempts": 0,
            "blocked": 0,
            "error": 0,
            "outcomes": Counter(),
            "blocked_by": Counter(),
            "interception_rate": 0.0,
        },
        "crashes": {"total": 0, "by_kind": Counter()},
        "unattended": {
            "sessions_total": 0,
            "sessions_human": 0,
            "issues_seen": 0,
            "issues_closed": 0,
            "issues_unattended": 0,
            "issues_settled": 0,
            "issues_in_progress": 0,
            "rate": None,
            "closed_loop_rate": None,
            "rate_settled": None,
        },
        # 闭环效率: wall-clock time from an issue's first event to its
        # first successful agent session, and cost rollups per issue.
        "closed_loop_time": _duration_stats([]),
        "cost": {
            "closed_issues": 0,
            "closed_total_usd": 0.0,
            "closed_avg_usd": None,
            "top_issues": [],
        },
        # 重试自愈: retryable (automatic) failures followed by a success
        # on the same issue — did the loop recover without a human?
        "retry": {
            "retryable_failures": 0,
            "issues_self_healed": 0,
            "avg_retries_to_success": None,
        },
        # 干预分析: for human-intervention sessions, what went wrong before?
        "intervention": {
            "human_sessions": 0,
            "with_prior_errors": 0,
            "avg_prior_errors": None,
            "prior_error_reasons": Counter(),
        },
        # 时间维度: concurrency sweep over agent session intervals,
        # hourly event histogram.
        "concurrency": {"max": 0, "avg": None},
        "hourly": Counter(),
        "turn_duration": _duration_stats([]),
    }

    durations: list[float] = []
    e2e_durations: list[float] = []
    queue_waits: list[float] = []
    first_events: list[float] = []
    first_turns: list[float] = []
    turn_durations: list[float] = []
    # Per-issue closed-loop bookkeeping over agent session_end events.
    issue_seen: set[str] = set()
    issue_closed: set[str] = set()
    issue_human: set[str] = set()
    # 闭环效率 / 干预分析 / 时间维度 bookkeeping.
    issue_first_ts: dict[str, float] = {}
    issue_success_ts: dict[str, float] = {}
    issue_cost_usd: dict[str, float] = {}
    issue_retryable_fails: dict[str, int] = {}
    issue_settled_last: dict[str, bool] = {}
    start_ts_by_session: dict[str, float] = {}
    session_intervals: list[tuple[float, float]] = []
    human_session_ids: list[str] = []
    errors_by_session: Counter = Counter()
    error_reasons_by_session: dict[str, Counter] = {}
    hourly: Counter = Counter()

    def backend_bucket(backend: Any) -> dict[str, Any]:
        return summary["by_backend"].setdefault(
            str(backend or "unknown"),
            {
                "sessions": 0,
                "succeeded": 0,
                "failed": 0,
                "tokens_input": 0,
                "tokens_output": 0,
                "cost_usd": 0.0,
                "duration_s": 0.0,
                "turns": 0,
            },
        )

    def model_bucket(model: Any) -> dict[str, Any]:
        return summary["by_model"].setdefault(
            str(model or "unknown"),
            {"usage_events": 0, "tokens_input": 0, "tokens_output": 0, "cost_usd": 0.0},
        )

    for ev in events:
        etype = ev.get("type")
        payload = ev.get("payload") or {}
        issue = str(ev.get("issue_id") or "")
        ts = float(ev.get("ts") or 0.0)
        if ts > 0:
            hourly[time.localtime(ts).tm_hour] += 1
        if issue:
            summary["by_issue"][issue] += 1

        if etype == "session_start":
            summary["sessions"] += 1
            sid = str(ev.get("session_id") or "")
            if sid and sid not in start_ts_by_session:
                start_ts_by_session[sid] = ts
        elif etype == "command_run":
            summary["commands"] += 1
        elif etype == "error":
            summary["errors"] += 1
            summary["errors_by_reason"][str(payload.get("reason") or "unknown")] += 1
            sid = str(ev.get("session_id") or "")
            if sid:
                errors_by_session[sid] += 1
                error_reasons_by_session.setdefault(sid, Counter())[
                    str(payload.get("reason") or "unknown")
                ] += 1
        elif etype == "session_end":
            summary["sessions_ended"] += 1
            status = payload.get("exit_status")
            ok = status in (None, 0, "0")
            if ok:
                summary["sessions_succeeded"] += 1
            else:
                summary["sessions_failed"] += 1
                summary["end_reasons_failed"][
                    str(payload.get("end_reason") or "unknown")
                ] += 1

            # Agent runs carry turn_count; the daemon's own session_end
            # (orchestrator polling loop) never does. Duration stats over
            # agent runs only — a daemon session spans the whole day.
            if "turn_count" in payload:
                end_reason = str(payload.get("end_reason") or "")
                sid = str(ev.get("session_id") or "")
                unattended = summary["unattended"]
                unattended["sessions_total"] += 1
                if issue:
                    issue_seen.add(issue)
                    issue_first_ts.setdefault(issue, ts)
                    if ok:
                        issue_success_ts.setdefault(issue, ts)
                    issue_settled_last[issue] = bool(ok) or (
                        end_reason in _HUMAN_END_REASONS
                    )
                    if end_reason in _HUMAN_END_REASONS:
                        issue_human.add(issue)
                    elif ok is False and end_reason:
                        # Automatic failure (backend error / watchdog /
                        # preflight…) — the daemon will retry on its own.
                        issue_retryable_fails[issue] = (
                            issue_retryable_fails.get(issue, 0) + 1
                        )
                if end_reason in _HUMAN_END_REASONS:
                    unattended["sessions_human"] += 1
                    human_session_ids.append(sid)
                bucket = backend_bucket(ev.get("backend") or payload.get("backend"))
                bucket["sessions"] += 1
                if ok:
                    bucket["succeeded"] += 1
                    if issue:
                        issue_closed.add(issue)
                else:
                    bucket["failed"] += 1
                duration = _num(payload.get("duration_s"))
                if duration is not None:
                    durations.append(duration)
                    bucket["duration_s"] += duration
                    # Concurrency interval; approximate the start when no
                    # matching session_start was recorded.
                    start_ts = start_ts_by_session.get(sid, ts - duration)
                    session_intervals.append((start_ts, ts))
                queue_wait = _num(payload.get("queue_wait_s"))
                if queue_wait is not None and duration is not None:
                    queue_waits.append(queue_wait)
                    e2e_durations.append(queue_wait + duration)
                first_event = _num(payload.get("first_event_latency_s"))
                if first_event is not None:
                    first_events.append(first_event)
                first_turn = _num(payload.get("first_turn_latency_s"))
                if first_turn is not None:
                    first_turns.append(first_turn)
                paused = _num(payload.get("paused_s"))
                if paused is not None:
                    summary["paused_s"] += paused
                backoff = _num(payload.get("backoff_429_s"))
                if backoff is not None:
                    summary["backoff_429_s"] += backoff
                turns = payload.get("turn_count")
                if isinstance(turns, (int, float)):
                    bucket["turns"] += int(turns)
                    summary["turns"]["total"] += int(turns)
                for name, stat in (payload.get("tools") or {}).items():
                    if not isinstance(stat, dict):
                        continue
                    tool = summary["tools"].setdefault(
                        str(name), {"calls": 0, "failures": 0, "duration_ms": 0.0}
                    )
                    tool["calls"] += int(_num(stat.get("calls")) or 0)
                    tool["failures"] += int(_num(stat.get("failures")) or 0)
                    tool["duration_ms"] += _num(stat.get("duration_ms")) or 0.0
        elif etype == "turn":
            summary["turns"]["turn_events"] += 1
            duration = _num(payload.get("duration_s"))
            if duration is not None:
                turn_durations.append(duration)
        elif etype == "verification":
            gate = summary["verification"]
            gate["attempts"] += 1
            outcome = str(payload.get("outcome") or "unknown")
            gate["outcomes"][outcome] += 1
            if outcome == "blocked":
                gate["blocked"] += 1
                gate["blocked_by"][
                    str(payload.get("blocked_by") or "unknown")
                ] += 1
            elif outcome == "error":
                gate["error"] += 1
        elif etype == "crash":
            crashes = summary["crashes"]
            crashes["total"] += 1
            crashes["by_kind"][str(payload.get("kind") or "unknown")] += 1
        elif etype == "usage":
            summary["usage_events"] += 1
            cost = payload.get("cost_usd")
            if not isinstance(cost, (int, float)):
                # Same key-shape variance as tokens below: some backends
                # report cost as totalCostUsd / total_cost_usd.
                cost = payload.get("totalCostUsd", payload.get("total_cost_usd"))
            cost_f = float(cost) if isinstance(cost, (int, float)) else 0.0
            summary["total_cost_usd"] += cost_f
            if issue:
                issue_cost_usd[issue] = issue_cost_usd.get(issue, 0.0) + cost_f
            tokens = payload.get("token_usage") or {}
            tokens_in = 0
            tokens_out = 0
            if isinstance(tokens, dict):
                # Backends report usage keys in different shapes (input /
                # input_tokens / inputTokens) — normalize here so historical
                # files written before the canonical form still aggregate.
                tokens_in = int(
                    tokens.get(
                        "input", tokens.get("input_tokens", tokens.get("inputTokens", 0))
                    )
                    or 0
                )
                tokens_out = int(
                    tokens.get(
                        "output",
                        tokens.get("output_tokens", tokens.get("outputTokens", 0)),
                    )
                    or 0
                )
            summary["tokens_input"] += tokens_in
            summary["tokens_output"] += tokens_out
            backend = backend_bucket(ev.get("backend") or payload.get("backend"))
            backend["tokens_input"] += tokens_in
            backend["tokens_output"] += tokens_out
            backend["cost_usd"] += cost_f
            model = model_bucket(ev.get("model") or payload.get("model"))
            model["usage_events"] += 1
            model["tokens_input"] += tokens_in
            model["tokens_output"] += tokens_out
            model["cost_usd"] += cost_f

    summary["session_duration"] = _duration_stats(durations)
    summary["session_e2e"] = _duration_stats(e2e_durations)
    latency = summary["latency"]
    latency["queue_wait_avg_s"] = (
        sum(queue_waits) / len(queue_waits) if queue_waits else 0.0
    )
    latency["first_event_avg_s"] = (
        sum(first_events) / len(first_events) if first_events else 0.0
    )
    latency["first_turn_avg_s"] = (
        sum(first_turns) / len(first_turns) if first_turns else 0.0
    )
    turns = summary["turns"]
    turns["total_s"] = sum(turn_durations)
    turns["avg_s"] = (
        turns["total_s"] / turns["turn_events"] if turns["turn_events"] else 0.0
    )
    summary["turn_duration"] = _duration_stats(turn_durations)
    verification = summary["verification"]
    if verification["attempts"]:
        verification["interception_rate"] = (
            verification["blocked"] / verification["attempts"]
        )
    agent_sessions = sum(
        b.get("sessions", 0) for b in summary["by_backend"].values()
    )
    crashes = summary["crashes"]
    crashes["backend_worker"] = crashes["by_kind"].get("backend_worker", 0)
    crashes["backend_rate_per_session"] = (
        crashes["backend_worker"] / agent_sessions if agent_sessions else None
    )
    unattended = summary["unattended"]
    unattended["issues_seen"] = len(issue_seen)
    unattended["issues_closed"] = len(issue_closed)
    unattended["issues_unattended"] = len(issue_closed - issue_human)
    if issue_seen:
        unattended["closed_loop_rate"] = len(issue_closed) / len(issue_seen)
        unattended["rate"] = (
            len(issue_closed - issue_human) / len(issue_seen)
        )
    # 口径修正: an issue whose LAST agent session of the day still ended
    # in an automatic failure is likely sitting in the retry queue (it
    # may close tomorrow) — report the unattended rate over settled
    # issues (last session succeeded or was human-stopped) as well.
    settled = {i for i in issue_seen if issue_settled_last.get(i)}
    unattended["issues_settled"] = len(settled)
    unattended["issues_in_progress"] = len(issue_seen - settled)
    if settled:
        unattended["rate_settled"] = (
            len((issue_closed - issue_human) & settled) / len(settled)
        )

    # 闭环效率: first agent session end → first success per issue.
    closed_times = [
        issue_success_ts[i] - issue_first_ts[i]
        for i in issue_success_ts
        if i in issue_first_ts and issue_success_ts[i] >= issue_first_ts[i]
    ]
    summary["closed_loop_time"] = _duration_stats(closed_times)
    cost = summary["cost"]
    closed_total = sum(issue_cost_usd.get(i, 0.0) for i in issue_closed)
    cost["closed_issues"] = len(issue_closed)
    cost["closed_total_usd"] = closed_total
    cost["closed_avg_usd"] = (
        closed_total / len(issue_closed) if issue_closed else None
    )
    cost["top_issues"] = [
        {"issue": k, "cost_usd": round(v, 4)}
        for k, v in sorted(issue_cost_usd.items(), key=lambda kv: -kv[1])[:5]
        if v > 0
    ]

    # 重试自愈: issues that ate ≥1 automatic failure and still closed.
    healed = [i for i in issue_closed if issue_retryable_fails.get(i, 0) > 0]
    retry = summary["retry"]
    retry["retryable_failures"] = sum(issue_retryable_fails.values())
    retry["issues_self_healed"] = len(healed)
    retry["avg_retries_to_success"] = (
        sum(issue_retryable_fails[i] for i in healed) / len(healed)
        if healed
        else None
    )

    # 干预分析: what preceded each human takeover?
    intervention = summary["intervention"]
    intervention["human_sessions"] = len(human_session_ids)
    prior_counts = [errors_by_session.get(s, 0) for s in human_session_ids]
    intervention["with_prior_errors"] = sum(1 for c in prior_counts if c > 0)
    intervention["avg_prior_errors"] = (
        sum(prior_counts) / len(prior_counts) if prior_counts else None
    )
    prior_reasons: Counter = Counter()
    for s in human_session_ids:
        prior_reasons.update(error_reasons_by_session.get(s, {}))
    intervention["prior_error_reasons"] = prior_reasons

    # 并发度: sweep-line over agent session intervals.
    sweep: list[tuple[float, int]] = []
    for start, end in session_intervals:
        if end > start:
            sweep.append((start, 1))
            sweep.append((end, -1))
    sweep.sort()
    concurrency = summary["concurrency"]
    if sweep:
        running = 0
        prev_ts = sweep[0][0]
        integral = 0.0
        for point_ts, delta in sweep:
            integral += running * (point_ts - prev_ts)
            running += delta
            concurrency["max"] = max(concurrency["max"], running)
            prev_ts = point_ts
        span = sweep[-1][0] - sweep[0][0]
        concurrency["avg"] = integral / span if span > 0 else float(concurrency["max"])
    summary["hourly"] = hourly
    return summary


def _fmt(values: dict[str, Any], key: str, unit: str = "s") -> str:
    value = values.get(key, 0.0)
    return f"{value:.1f}{unit}"


# Display-only exchange rate: telemetry records raw USD cost (cost_usd);
# the rendered report shows CNY. Summary dict values stay in USD.
USD_TO_CNY_RATE = 7.1


def _cny(usd: float) -> str:
    return f"¥{usd * USD_TO_CNY_RATE:,.2f}"


def render_summary_markdown(summary: dict[str, Any], *, env_label: str = "") -> str:
    """Render an aggregated summary as the Markdown body for the issue report."""
    header = "Orchestratord Telemetry" + (f" [{env_label}]" if env_label else "")
    issue_counts = ", ".join(
        f"#{k}: {v}" for k, v in sorted(summary.get("by_issue", {}).items())
    ) or "-"
    lines = [
        f"# {header} — {summary.get('date', '')}",
        "",
        "## 汇总",
        "",
        "| 指标 | 值 |",
        "|------|-----|",
        f"| 事件总数 | {summary.get('events', 0)} |",
        f"| session 数 | {summary.get('sessions', 0)} |",
        f"| session 结束数 | {summary.get('sessions_ended', 0)} |",
        f"| command 数 | {summary.get('commands', 0)} |",
        f"| error 数 | {summary.get('errors', 0)} |",
        f"| session 成功/失败 | {summary.get('sessions_succeeded', 0)} / {summary.get('sessions_failed', 0)} |",
        f"| usage 事件 | {summary.get('usage_events', 0)} |",
        f"| 总成本 (CNY) | {_cny(summary.get('total_cost_usd', 0.0))} |",
        f"| tokens (in/out) | {summary.get('tokens_input', 0)} / {summary.get('tokens_output', 0)} |",
        "",
        "## 按 issue 事件分布",
        "",
        issue_counts,
        "",
    ]

    unattended = summary.get("unattended") or {}
    if unattended.get("issues_seen"):
        closed_rate = unattended.get("closed_loop_rate") or 0.0
        rate = unattended.get("rate") or 0.0
        lines += [
            "## 无人干预闭环",
            "",
            "| 指标 | 值 |",
            "|------|-----|",
            f"| 涉及 issue 数 | {unattended.get('issues_seen', 0)} |",
            f"| 闭环 issue 数（当日有成功会话） | {unattended.get('issues_closed', 0)} |",
            f"| 无人干预闭环 issue 数 | {unattended.get('issues_unattended', 0)} |",
            f"| 闭环率 | {closed_rate * 100:.1f}% |",
            f"| 无人干预闭环率 | {rate * 100:.1f}% |",
            f"| 人工干预会话数 | {unattended.get('sessions_human', 0)} |",
        ]
        if unattended.get("issues_settled"):
            rate_settled = unattended.get("rate_settled")
            rate_settled_text = (
                f"{rate_settled * 100:.1f}%" if rate_settled is not None else "-"
            )
            lines += [
                f"| 进行中 issue 数（当日仍以自动失败收尾） | {unattended.get('issues_in_progress', 0)} |",
                f"| 无人干预闭环率（排除进行中 issue） | {rate_settled_text} |",
            ]
        lines.append("")

    closed_loop_time = summary.get("closed_loop_time") or {}
    cost = summary.get("cost") or {}
    retry = summary.get("retry") or {}
    has_efficiency = (
        closed_loop_time.get("count")
        or cost.get("closed_issues")
        or retry.get("retryable_failures")
    )
    if has_efficiency:
        lines += [
            "## 闭环效率",
            "",
            "| 指标 | 值 |",
            "|------|-----|",
        ]
        if closed_loop_time.get("count"):
            lines.append(
                f"| 闭环耗时 avg / 中位 / p95 | {_fmt(closed_loop_time, 'avg_s')} / {_fmt(closed_loop_time, 'p50_s')} / {_fmt(closed_loop_time, 'p95_s')} |"
            )
        if cost.get("closed_issues"):
            closed_avg = cost.get("closed_avg_usd")
            closed_avg_text = (
                _cny(closed_avg) if closed_avg is not None else "-"
            )
            lines.append(
                f"| 闭环 issue 成本 avg / 合计 | {closed_avg_text} / {_cny(cost.get('closed_total_usd', 0.0))} |"
            )
            top_issues = cost.get("top_issues") or []
            if top_issues:
                top_text = ", ".join(
                    f"#{item['issue']}: {_cny(item['cost_usd'])}" for item in top_issues
                )
                lines.append(f"| 成本 Top {len(top_issues)} issue | {top_text} |")
        if retry.get("retryable_failures"):
            avg_retries = retry.get("avg_retries_to_success")
            avg_retries_text = f"{avg_retries:.1f}" if avg_retries is not None else "-"
            lines.append(
                f"| 自动失败次数 / 自愈 issue 数 / 平均重试到成功 | {retry.get('retryable_failures', 0)} / {retry.get('issues_self_healed', 0)} / {avg_retries_text} |"
            )
        lines.append("")

    intervention = summary.get("intervention") or {}
    if intervention.get("human_sessions"):
        avg_prior = intervention.get("avg_prior_errors")
        avg_prior_text = f"{avg_prior:.1f}" if avg_prior is not None else "-"
        lines += [
            "## 人工干预",
            "",
            "| 指标 | 值 |",
            "|------|-----|",
            f"| 干预会话数 | {intervention.get('human_sessions', 0)} |",
            f"| 有前置错误的干预会话 | {intervention.get('with_prior_errors', 0)} |",
            f"| 平均前置错误数 | {avg_prior_text} |",
        ]
        prior_reasons = intervention.get("prior_error_reasons") or {}
        if prior_reasons:
            top_reasons = sorted(prior_reasons.items(), key=lambda kv: -kv[1])[:3]
            reasons_text = ", ".join(f"{reason}×{count}" for reason, count in top_reasons)
            lines.append(f"| 前置错误 Top 3 | {reasons_text} |")
        lines.append("")

    duration = summary.get("session_duration") or {}
    if duration.get("count"):
        e2e = summary.get("session_e2e") or {}
        latency = summary.get("latency") or {}
        turns = summary.get("turns") or {}
        lines += [
            "## 耗时 (agent 会话)",
            "",
            "| 指标 | 值 |",
            "|------|-----|",
            f"| 会话耗时 avg / Q1 / 中位 / Q3 | {_fmt(duration, 'avg_s')} / {_fmt(duration, 'p25_s')} / {_fmt(duration, 'p50_s')} / {_fmt(duration, 'p75_s')} |",
            f"| 会话耗时 p95 / max | {_fmt(duration, 'p95_s')} / {_fmt(duration, 'max_s')} |",
            f"| 端到端 avg / Q1 / 中位 / Q3 | {_fmt(e2e, 'avg_s')} / {_fmt(e2e, 'p25_s')} / {_fmt(e2e, 'p50_s')} / {_fmt(e2e, 'p75_s')} |",
            f"| 端到端 p95 / max | {_fmt(e2e, 'p95_s')} / {_fmt(e2e, 'max_s')} |",
            f"| 排队等待 avg | {_fmt(latency, 'queue_wait_avg_s')} |",
            f"| 首事件延迟 avg | {_fmt(latency, 'first_event_avg_s')} |",
            f"| 首 turn 延迟 avg | {_fmt(latency, 'first_turn_avg_s')} |",
            f"| turns 总数 / turn 事件 / turn 均耗时 | {turns.get('total', 0)} / {turns.get('turn_events', 0)} / {_fmt(turns, 'avg_s')} |",
            f"| 暂停总时长 / 429 退避总时长 | {summary.get('paused_s', 0.0):.1f}s / {summary.get('backoff_429_s', 0.0):.1f}s |",
        ]
        concurrency = summary.get("concurrency") or {}
        if concurrency.get("max"):
            avg_conc = concurrency.get("avg")
            avg_conc_text = f"{avg_conc:.1f}" if avg_conc is not None else "-"
            lines.append(f"| 并发度 max / avg | {concurrency.get('max', 0)} / {avg_conc_text} |")
        turn_duration = summary.get("turn_duration") or {}
        if turn_duration.get("count"):
            lines.append(
                f"| turn 耗时 avg / Q1 / 中位 / Q3 | {_fmt(turn_duration, 'avg_s')} / {_fmt(turn_duration, 'p25_s')} / {_fmt(turn_duration, 'p50_s')} / {_fmt(turn_duration, 'p75_s')} |"
            )
        turns_total = (summary.get("turns") or {}).get("total", 0)
        tokens_in_total = summary.get("tokens_input", 0)
        if turns_total and tokens_in_total:
            lines.append(f"| tokens (in) / turn | {tokens_in_total // turns_total} |")
        lines.append("")

    by_backend = summary.get("by_backend") or {}
    if by_backend:
        lines += [
            "## 按后端",
            "",
            "| backend | 会话 | 成功/失败 | tokens (in/out) | 成本 CNY | 执行时长 | turns |",
            "|---------|------|-----------|-----------------|----------|----------|-------|",
        ]
        for name in sorted(by_backend):
            b = by_backend[name]
            lines.append(
                f"| {name} | {b.get('sessions', 0)} "
                f"| {b.get('succeeded', 0)} / {b.get('failed', 0)} "
                f"| {b.get('tokens_input', 0)} / {b.get('tokens_output', 0)} "
                f"| {_cny(b.get('cost_usd', 0.0))} "
                f"| {b.get('duration_s', 0.0):.1f}s "
                f"| {b.get('turns', 0)} |"
            )
        lines.append("")

    end_reasons = summary.get("end_reasons_failed") or {}
    if end_reasons:
        lines += [
            "## 失败原因 (session_end)",
            "",
            "| end_reason | 次数 |",
            "|------------|------|",
        ]
        for reason, count in sorted(end_reasons.items(), key=lambda kv: -kv[1]):
            lines.append(f"| {reason} | {count} |")
        lines.append("")

    error_reasons = summary.get("errors_by_reason") or {}
    if error_reasons:
        lines += [
            "## 错误事件原因",
            "",
            "| 原因 | 次数 |",
            "|------|------|",
        ]
        for reason, count in sorted(error_reasons.items(), key=lambda kv: -kv[1]):
            lines.append(f"| {reason} | {count} |")
        lines.append("")

    tools = summary.get("tools") or {}
    if tools:
        lines += [
            "## 工具调用 Top 10",
            "",
            "| 工具 | 调用 | 失败 | 失败率 | 总耗时 | 均耗时 |",
            "|------|------|------|--------|--------|--------|",
        ]
        top = sorted(tools.items(), key=lambda kv: -kv[1].get("calls", 0))[:10]
        for name, stat in top:
            calls = stat.get("calls", 0)
            failures = stat.get("failures", 0)
            duration_ms = stat.get("duration_ms", 0.0)
            failure_rate = f"{failures / calls * 100:.1f}%" if calls else "-"
            avg_ms = f"{duration_ms / calls:.0f}ms" if calls else "-"
            lines.append(
                f"| {name} | {calls} "
                f"| {failures} "
                f"| {failure_rate} "
                f"| {duration_ms:.0f}ms "
                f"| {avg_ms} |"
            )
        lines.append("")

    hourly = summary.get("hourly") or {}
    if hourly:
        lines += [
            "## 时段分布",
            "",
            "| 时段 | 事件数 |",
            "|------|--------|",
        ]
        for hour in sorted(hourly):
            lines.append(f"| {hour:02d}:00-{hour:02d}:59 | {hourly[hour]} |")
        lines.append("")

    by_model = summary.get("by_model") or {}
    if by_model:
        lines += [
            "## 按模型 (usage)",
            "",
            "| model | usage 事件 | tokens (in/out) | 成本 CNY |",
            "|-------|------------|-----------------|----------|",
        ]
        for name in sorted(by_model):
            m = by_model[name]
            lines.append(
                f"| {name} | {m.get('usage_events', 0)} "
                f"| {m.get('tokens_input', 0)} / {m.get('tokens_output', 0)} "
                f"| {_cny(m.get('cost_usd', 0.0))} |"
            )
        lines.append("")

    verification = summary.get("verification") or {}
    if verification.get("attempts"):
        rate = verification.get("interception_rate") or 0.0
        lines += [
            "## 验证门",
            "",
            "| 指标 | 值 |",
            "|------|-----|",
            f"| 验证尝试 | {verification.get('attempts', 0)} |",
            f"| 拦截 (blocked) | {verification.get('blocked', 0)} |",
            f"| 缺陷拦截率 | {rate * 100:.1f}% |",
            f"| 错误 (error) | {verification.get('error', 0)} |",
            "",
        ]
        blocked_by = verification.get("blocked_by") or {}
        if blocked_by:
            lines += [
                "| 拦截原因 | 次数 |",
                "|----------|------|",
            ]
            for reason, count in sorted(blocked_by.items(), key=lambda kv: -kv[1]):
                lines.append(f"| {reason} | {count} |")
            lines.append("")

    crashes = summary.get("crashes") or {}
    if crashes.get("total"):
        backend_rate = crashes.get("backend_rate_per_session")
        rate_text = (
            f"{backend_rate:.3f}/session" if backend_rate is not None else "-"
        )
        lines += [
            "## 崩溃",
            "",
            "| 指标 | 值 |",
            "|------|-----|",
            f"| 崩溃总数 | {crashes.get('total', 0)} |",
            f"| backend worker 崩溃 | {crashes.get('backend_worker', 0)} ({rate_text}) |",
            "",
        ]
        by_kind = crashes.get("by_kind") or {}
        if by_kind:
            lines += [
                "| 类型 | 次数 |",
                "|------|------|",
            ]
            for kind, count in sorted(by_kind.items(), key=lambda kv: -kv[1]):
                lines.append(f"| {kind} | {count} |")
            lines.append("")

    return "\n".join(lines)
