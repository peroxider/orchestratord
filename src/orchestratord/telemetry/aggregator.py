"""Telemetry aggregator — daily run summary from the local event store.

Reads the day's events (``~/.orchestratord/telemetry/events/<date>.jsonl``)
via :func:`orchestratord.telemetry.storage.read_events` and reduces them
into a per-day summary: run/session counts, success/failure, total usage
(tokens / cost) — the payload a reporter turns into a Markdown issue body.
"""

from __future__ import annotations

import time
from collections import Counter
from typing import Any

from .storage import read_events


def _day_key() -> str:
    return time.strftime("%Y-%m-%d")


def aggregate_day(day: str | None = None) -> dict[str, Any]:
    """Aggregate one day of events into a summary dict.

    Keys: date, sessions, commands, errors, usage_events, runs_succeeded,
    runs_failed, sessions_by_status, total_cost_usd, tokens (input/output
    summed across usage events when present), by_issue.
    """
    day = day or _day_key()
    events = read_events(day)

    summary: dict[str, Any] = {
        "date": day,
        "events": len(events),
        "sessions": 0,
        "commands": 0,
        "errors": 0,
        "usage_events": 0,
        "sessions_succeeded": 0,
        "sessions_failed": 0,
        "total_cost_usd": 0.0,
        "tokens_input": 0,
        "tokens_output": 0,
        "by_issue": Counter(),
    }

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
        elif etype == "session_end":
            status = payload.get("exit_status")
            ok = status in (None, 0, "0")
            if ok:
                summary["sessions_succeeded"] += 1
            else:
                summary["sessions_failed"] += 1
        elif etype == "usage":
            summary["usage_events"] += 1
            cost = payload.get("cost_usd")
            if isinstance(cost, (int, float)):
                summary["total_cost_usd"] += float(cost)
            tokens = payload.get("token_usage") or {}
            if isinstance(tokens, dict):
                summary["tokens_input"] += int(tokens.get("input", tokens.get("input_tokens", 0)) or 0)
                summary["tokens_output"] += int(tokens.get("output", tokens.get("output_tokens", 0)) or 0)
    return summary


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
    return "\n".join(lines)
