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
    Enriched keys: session_duration / session_e2e distributions, latency
    (queue/first-event/first-turn averages), paused_s / backoff_429_s
    totals, turns, by_backend, by_model, end_reasons_failed,
    errors_by_reason, tools.
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
            "rate": None,
            "closed_loop_rate": None,
        },
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
        if issue:
            summary["by_issue"][issue] += 1

        if etype == "session_start":
            summary["sessions"] += 1
        elif etype == "command_run":
            summary["commands"] += 1
        elif etype == "error":
            summary["errors"] += 1
            summary["errors_by_reason"][str(payload.get("reason") or "unknown")] += 1
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
                unattended = summary["unattended"]
                unattended["sessions_total"] += 1
                if issue:
                    issue_seen.add(issue)
                    if end_reason in _HUMAN_END_REASONS:
                        issue_human.add(issue)
                if end_reason in _HUMAN_END_REASONS:
                    unattended["sessions_human"] += 1
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
            cost_f = float(cost) if isinstance(cost, (int, float)) else 0.0
            summary["total_cost_usd"] += cost_f
            tokens = payload.get("token_usage") or {}
            tokens_in = 0
            tokens_out = 0
            if isinstance(tokens, dict):
                tokens_in = int(tokens.get("input", tokens.get("input_tokens", 0)) or 0)
                tokens_out = int(tokens.get("output", tokens.get("output_tokens", 0)) or 0)
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
    return summary


def _fmt(values: dict[str, Any], key: str, unit: str = "s") -> str:
    value = values.get(key, 0.0)
    return f"{value:.1f}{unit}"


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
        f"| 总成本 (USD) | {summary.get('total_cost_usd', 0.0):.4f} |",
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
            "",
        ]

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
            "",
        ]

    by_backend = summary.get("by_backend") or {}
    if by_backend:
        lines += [
            "## 按后端",
            "",
            "| backend | 会话 | 成功/失败 | tokens (in/out) | 成本 USD | 执行时长 | turns |",
            "|---------|------|-----------|-----------------|----------|----------|-------|",
        ]
        for name in sorted(by_backend):
            b = by_backend[name]
            lines.append(
                f"| {name} | {b.get('sessions', 0)} "
                f"| {b.get('succeeded', 0)} / {b.get('failed', 0)} "
                f"| {b.get('tokens_input', 0)} / {b.get('tokens_output', 0)} "
                f"| {b.get('cost_usd', 0.0):.4f} "
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
            "| 工具 | 调用 | 失败 | 总耗时 |",
            "|------|------|------|--------|",
        ]
        top = sorted(tools.items(), key=lambda kv: -kv[1].get("calls", 0))[:10]
        for name, stat in top:
            lines.append(
                f"| {name} | {stat.get('calls', 0)} "
                f"| {stat.get('failures', 0)} "
                f"| {stat.get('duration_ms', 0.0):.0f}ms |"
            )
        lines.append("")

    by_model = summary.get("by_model") or {}
    if by_model:
        lines += [
            "## 按模型 (usage)",
            "",
            "| model | usage 事件 | tokens (in/out) | 成本 USD |",
            "|-------|------------|-----------------|----------|",
        ]
        for name in sorted(by_model):
            m = by_model[name]
            lines.append(
                f"| {name} | {m.get('usage_events', 0)} "
                f"| {m.get('tokens_input', 0)} / {m.get('tokens_output', 0)} "
                f"| {m.get('cost_usd', 0.0):.4f} |"
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
