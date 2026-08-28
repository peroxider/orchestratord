"""orchestrator dashboard — standalone LiveView UI.

Usage:
  orchestratord dashboard --port 8080 [--workspace PATH]

Launches a standalone HTTP server that streams real-time orchestrator events
to a web-based dashboard. Agents push events to a local event log, and the
dashboard server reads these logs to render a web UI.
"""

from __future__ import annotations

import argparse
import atexit
import json
import logging
import os
import queue
import signal
import sys
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

from ..event_tailer import EventTailerManager  # noqa: E402
from ..chat_gateway import ChatGateway  # noqa: E402
from ..paths import ORCHESTRATOR_DIR, ORCHESTRATORD_BASE


# ---------------------------------------------------------------------------
# Issue status taxonomy
# ---------------------------------------------------------------------------
# These constants MUST stay in sync with extensions.orchestrator.issue_registry
# .IssueStatus. We re-declare them here so the dashboard server can be loaded
# even when the orchestrator is not installed in the same interpreter, and so
# the strings are stable for the frontend.

ISSUE_STATUSES: tuple[str, ...] = (
    "queued",
    "pending",
    "running",
    "synced",
    "pending_review",
    "completed",
    "failed",
    "abandoned",
    "verification_failed",
)

STATUS_META: dict[str, dict[str, str]] = {
    "queued": {"label": "Queued", "color": "#6e7681", "icon": "◷", "group": "active"},
    "pending": {"label": "Pending", "color": "#d29922", "icon": "○", "group": "active"},
    "running": {"label": "Running", "color": "#58a6ff", "icon": "◉", "group": "active"},
    "synced": {"label": "Synced", "color": "#a371f7", "icon": "⇄", "group": "active"},
    "pending_review": {"label": "Review", "color": "#79c0ff", "icon": "◎", "group": "active"},
    "completed": {"label": "Completed", "color": "#3fb950", "icon": "✓", "group": "terminal"},
    "failed": {"label": "Failed", "color": "#f85149", "icon": "✗", "group": "terminal"},
    "abandoned": {"label": "Abandoned", "color": "#8b949e", "icon": "⊘", "group": "terminal"},
    "verification_failed": {
        "label": "Verify Failed",
        "color": "#db6d28",
        "icon": "⚠",
        "group": "terminal",
    },
}

ACTIVE_STATUSES = {s for s, m in STATUS_META.items() if m["group"] == "active"}
TERMINAL_STATUSES = {s for s, m in STATUS_META.items() if m["group"] == "terminal"}


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


def add_dashboard_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "dashboard",
        help="Launch standalone LiveView dashboard UI",
        description="Start an HTTP server with a web dashboard for real-time "
        "orchestrator monitoring. Streams running sessions, tool calls, "
        "and LLM responses.",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8080,
        help="Port to listen on (default: 8080)",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Host to bind to (default: 127.0.0.1)",
    )
    parser.add_argument(
        "--workspace",
        type=str,
        default=None,
        metavar="PATH",
        help="Workspace root to read registry/event logs from. "
        "If omitted, uses $ORCHESTRATORD_WORKSPACE_ROOT, falls back to the "
        "latest metadata under ~/.orchestratord/orchestrator/*/metadata.json, "
        "and finally ~/.orchestratord/workspace.",
    )
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="Do not auto-open the dashboard URL in a browser.",
    )


# ---------------------------------------------------------------------------
# Workspace resolution
# ---------------------------------------------------------------------------


def _resolve_workspace_root(explicit: str | None = None) -> Path:
    """Resolve the workspace root in priority order.

    1. Explicit --workspace argument.
    2. $ORCHESTRATORD_WORKSPACE_ROOT environment variable.
    3. Latest metadata.json under ~/.orchestratord/orchestrator/*/metadata.json.
    4. ~/.orchestratord/workspace (last-resort default).
    """
    if explicit:
        return Path(explicit).expanduser().resolve()

    env_ws = os.environ.get("ORCHESTRATORD_WORKSPACE_ROOT")
    if env_ws:
        return Path(env_ws).expanduser().resolve()

    metadata_dir = ORCHESTRATOR_DIR
    if metadata_dir.exists():
        candidates = []
        for md_dir in metadata_dir.iterdir():
            mf = md_dir / "metadata.json"
            if mf.exists():
                try:
                    data = json.loads(mf.read_text(encoding="utf-8"))
                    ws = data.get("workspace_root")
                    started = data.get("started_at") or 0
                    if ws:
                        candidates.append((started, Path(ws)))
                except Exception:
                    continue
        if candidates:
            candidates.sort(key=lambda c: c[0], reverse=True)
            return candidates[0][1]

    return ORCHESTRATORD_BASE / "workspace"


# ---------------------------------------------------------------------------
# State aggregation
# ---------------------------------------------------------------------------


def _safe_read_json(path: Path) -> dict[str, Any] | None:
    try:
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _classify_status(value: Any) -> str:
    """Normalize a status value to one of ISSUE_STATUSES, defaulting to 'pending'."""
    if not isinstance(value, str):
        return "pending"
    v = value.strip().lower()
    return v if v in ISSUE_STATUSES else "pending"


def _gather_issue_metadata(workspace: Path) -> dict[str, Any]:
    """Read the IssueRegistry JSON and produce a structured per-issue view.

    Returns a dict shaped as:
      {
        "issues": [ {issue_id, identifier, status, ...}, ... ],
        "by_status": { "pending": n, "running": n, ... },
        "totals": { "total": n, "active": n, "terminal": n, "prs": n }
      }
    """
    registry_path = workspace / ".orchestratord_issue_registry.json"
    raw = _safe_read_json(registry_path) or {}

    issues: list[dict[str, Any]] = []
    by_status: dict[str, int] = {s: 0 for s in ISSUE_STATUSES}

    now = time.time()
    for issue_id, record in raw.items():
        if not isinstance(record, dict):
            continue
        status = _classify_status(record.get("status"))
        by_status[status] += 1

        created_at = float(record.get("created_at") or 0)
        updated_at = float(record.get("updated_at") or 0)
        age_seconds = max(0, int(now - created_at)) if created_at else 0
        idle_seconds = max(0, int(now - updated_at)) if updated_at else 0

        workspace_path = record.get("workspace_path") or ""
        workspace_short = ""
        if workspace_path:
            try:
                workspace_short = (
                    "/" + str(Path(workspace_path).relative_to(workspace))
                    if workspace in Path(workspace_path).parents
                    else workspace_path
                )
            except Exception:
                workspace_short = workspace_path

        issues.append(
            {
                "issue_id": issue_id,
                "identifier": record.get("issue_identifier") or issue_id,
                "status": status,
                "branch_name": record.get("branch_name"),
                "commit_sha": record.get("commit_sha"),
                "pr_number": record.get("pr_number"),
                "pr_url": record.get("pr_url"),
                "base_branch": record.get("base_branch") or "main",
                "workspace_path": workspace_path,
                "workspace_short": workspace_short,
                "workspace_strategy": record.get("workspace_strategy"),
                "attempt_count": int(record.get("attempt_count") or 0),
                "retry_count": int(record.get("retry_count") or 0),
                "sequence_index": record.get("sequence_index"),
                "intent": record.get("intent") or "none",
                "report_path": record.get("report_path"),
                "verification_status": record.get("verification_status"),
                "clarification_status": record.get("clarification_status"),
                "created_at": created_at,
                "updated_at": updated_at,
                "age_seconds": age_seconds,
                "idle_seconds": idle_seconds,
                "run_id": record.get("run_id"),
                "run_turn_count": record.get("run_turn_count", 0),
                "run_tool_count": record.get("run_tool_count", 0),
            }
        )

    # Sort: active statuses first, then by most recent activity.
    issues.sort(
        key=lambda i: (
            0 if i["status"] in ACTIVE_STATUSES else 1,
            -int(i["updated_at"] or 0),
        )
    )

    return {
        "issues": issues,
        "by_status": by_status,
        "totals": {
            "total": len(issues),
            "active": sum(1 for i in issues if i["status"] in ACTIVE_STATUSES),
            "terminal": sum(1 for i in issues if i["status"] in TERMINAL_STATUSES),
            "prs": sum(1 for i in issues if i.get("pr_number")),
        },
    }


def _gather_metadata(workspace: Path) -> dict[str, Any]:
    """Read the orchestrator daemon metadata.json (PID, started_at, project)."""
    metadata_dir = ORCHESTRATOR_DIR
    if not metadata_dir.exists():
        return {"found": False}

    best: tuple[float, dict[str, Any], Path] | None = None
    for md_dir in metadata_dir.iterdir():
        mf = md_dir / "metadata.json"
        if not mf.exists():
            continue
        try:
            data = json.loads(mf.read_text(encoding="utf-8"))
        except Exception:
            continue
        if data.get("workspace_root") and str(data.get("workspace_root")) != str(workspace):
            continue
        started = float(data.get("started_at") or 0)
        if best is None or started > best[0]:
            best = (started, data, mf)

    if best is None:
        return {"found": False}

    _, data, mf = best
    pid = data.get("pid")
    alive = False
    if isinstance(pid, int):
        try:
            os.kill(pid, 0)
            alive = True
        except (OSError, ProcessLookupError):
            alive = False
    started_at = float(data.get("started_at") or 0)
    return {
        "found": True,
        "pid": pid,
        "alive": alive,
        "started_at": started_at,
        "uptime_seconds": max(0, int(time.time() - started_at)) if started_at else 0,
        "project_slug": data.get("project_slug") or "",
        "workflow_path": data.get("workflow_path") or "",
        "metadata_path": str(mf),
    }


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------


class DashboardState:
    """Per-process shared state for the dashboard HTTP server."""

    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace
        self.snapshot: dict[str, Any] = {
            "type": "snapshot",
            "ts": time.time(),
            "workspace": str(workspace),
            "workspace_exists": workspace.exists(),
            "metadata": _gather_metadata(workspace),
            "issues": _gather_issue_metadata(workspace),
            "events": {"total": 0, "by_type": {}, "recent": []},
            "token_activity": {
                "active_sessions": 0,
                "total_turns": 0,
                "total_tools": 0,
            },
        }
        self.last_snapshot_at: float = time.time()
        self.snapshot_interval: float = 0.5
        self._lock = threading.Lock()
        # Event tailer for live per-session events
        self.tailer_manager = EventTailerManager(workspace)
        atexit.register(self.tailer_manager.stop_all)
        # Chat gateway for real-time agent chat (control socket bridge)
        self.chat_gateway = ChatGateway()
        atexit.register(self.chat_gateway.stop)
        # Rolling event buffers
        self._recent_events: deque[dict[str, Any]] = deque(maxlen=200)
        self._event_by_type: dict[str, int] = {}
        # Track completed issues whose historical events have been loaded
        self._loaded_historical: set[str] = set()
        # Track run_ids that were ever actively tailed, to prevent
        # load_historical() from re-reading files the tailer already consumed
        self._ever_tailed: set[str] = set()

    def refresh_snapshot(self, force: bool = False) -> dict[str, Any]:
        now = time.time()
        with self._lock:
            if force or (now - self.last_snapshot_at) >= self.snapshot_interval:
                # 1. Read registry + metadata
                issues = _gather_issue_metadata(self.workspace)
                meta = _gather_metadata(self.workspace)

                # 2. Sync tailers: extract active run_id → (issue_id, workspace_path) mapping
                run_id_map: dict[str, tuple[str, Path]] = {}
                for issue in issues.get("issues", []):
                    rid = issue.get("run_id")
                    if rid and issue.get("status") in ACTIVE_STATUSES:
                        ws = issue.get("workspace_path")
                        if ws:
                            run_id_map[rid] = (issue["issue_id"], Path(ws))
                            self._ever_tailed.add(rid)
                self.tailer_manager.sync_active_run_ids(run_id_map)

                # 2c. Sync chat gateway connections for active runs.
                run_id_to_sock: dict[str, Path] = {}
                for rid, (_, ws_path) in run_id_map.items():
                    sock_path = Path(ws_path) / ".run_control" / f"{rid}.sock"
                    run_id_to_sock[rid] = sock_path
                self.chat_gateway.sync_active_run_ids(run_id_to_sock)

                # 2b. One-shot historical load for completed issues with run_id
                # that haven't been loaded yet AND were never actively tailed.
                for issue in issues.get("issues", []):
                    rid = issue.get("run_id")
                    if (
                        rid
                        and issue.get("status") not in ACTIVE_STATUSES
                        and rid not in self._loaded_historical
                        and rid not in self._ever_tailed
                    ):
                        ws = issue.get("workspace_path")
                        if ws:
                            self.tailer_manager.load_historical(rid, issue["issue_id"], Path(ws))
                            self._loaded_historical.add(rid)

                # 3. Drain events, update rolling buffers
                for evt in self.tailer_manager.drain_events():
                    self._recent_events.appendleft(evt)
                    et = evt.get("event_type", "unknown")
                    self._event_by_type[et] = self._event_by_type.get(et, 0) + 1

                # 4. Assemble snapshot with events + token_activity
                self.snapshot = {
                    "type": "snapshot",
                    "ts": time.time(),
                    "workspace": str(self.workspace),
                    "workspace_exists": self.workspace.exists(),
                    "metadata": meta,
                    "issues": issues,
                    "events": {
                        "total": sum(self._event_by_type.values()),
                        "by_type": dict(self._event_by_type),
                        "recent": list(self._recent_events)[:200],
                    },
                    "token_activity": self._build_token_activity(issues),
                }
                self.last_snapshot_at = now
            return self.snapshot

    def _build_token_activity(self, issues: dict[str, Any]) -> dict[str, Any]:
        """从 registry 计数组装 token_activity（活跃会话数 + turns/tools）。"""
        issue_list = issues.get("issues", [])
        all_with_run = [i for i in issue_list if i.get("run_id")]
        active = [i for i in all_with_run if i.get("status") in ACTIVE_STATUSES]
        return {
            "active_sessions": len(active),
            "total_turns": sum(i.get("run_turn_count", 0) for i in all_with_run),
            "total_tools": sum(i.get("run_tool_count", 0) for i in all_with_run),
        }


# JavaScript payload of the dashboard HTML. Kept in a module-level constant so
# Python syntax-checks the surrounding code independently of the JS body.
DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>ClawCodex Orchestrator LiveView</title>
  <style>
    :root {
      --bg-0: #0b0f17;
      --bg-1: #11161f;
      --bg-2: #161c26;
      --bg-3: #1d2532;
      --line: #232c3a;
      --line-soft: #1a212c;
      --fg-0: #e6edf3;
      --fg-1: #c9d1d9;
      --fg-2: #8b949e;
      --fg-3: #6e7681;
      --accent: #58a6ff;
      --accent-2: #79c0ff;
      --good: #3fb950;
      --warn: #d29922;
      --bad: #f85149;
      --vermillion: #db6d28;
      --purple: #a371f7;
      --cyan: #79c0ff;
      --gray: #8b949e;
      --shadow: 0 8px 24px rgba(0, 0, 0, 0.35);
    }
    * { box-sizing: border-box; }
    html, body {
      margin: 0; padding: 0;
      background: var(--bg-0);
      color: var(--fg-1);
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "Helvetica Neue",
                   "PingFang SC", "Microsoft YaHei", Arial, sans-serif;
      font-size: 13px;
      line-height: 1.45;
      min-height: 100vh;
    }
    body {
      background:
        radial-gradient(1200px 600px at 80% -10%, rgba(88,166,255,0.06), transparent 70%),
        radial-gradient(1000px 500px at -10% 110%, rgba(163,113,247,0.05), transparent 70%),
        var(--bg-0);
    }
    a { color: var(--accent); text-decoration: none; }
    a:hover { text-decoration: underline; }
    code, .mono { font-family: "JetBrains Mono", "SF Mono", Menlo, Consolas, monospace; }
    button { font-family: inherit; }

    /* Layout */
    .app {
      display: grid;
      grid-template-rows: auto auto 1fr auto;
      grid-template-columns: 1fr;
      min-height: 100vh;
      padding: 18px 22px 12px;
      gap: 14px;
    }

    /* Header */
    .header {
      display: flex; align-items: center; gap: 18px;
      padding: 14px 18px;
      background: linear-gradient(180deg, rgba(255,255,255,0.02), transparent), var(--bg-1);
      border: 1px solid var(--line);
      border-radius: 12px;
      box-shadow: var(--shadow);
    }
    .header .brand {
      display: flex; align-items: center; gap: 10px;
      font-weight: 600; font-size: 15px; color: var(--fg-0);
    }
    .header .brand .logo {
      width: 26px; height: 26px; border-radius: 7px;
      background: conic-gradient(from 200deg, #58a6ff, #a371f7, #3fb950, #58a6ff);
      box-shadow: 0 0 0 1px rgba(255,255,255,0.06) inset;
    }
    .header .meta { display: flex; gap: 18px; flex: 1; flex-wrap: wrap; }
    .header .meta .kv { color: var(--fg-2); font-size: 12px; }
    .header .meta .kv b { color: var(--fg-0); font-weight: 500; margin-left: 6px; }
    .conn {
      display: inline-flex; align-items: center; gap: 8px;
      padding: 6px 10px; border-radius: 999px;
      background: var(--bg-2); border: 1px solid var(--line);
      font-size: 12px;
    }
    .conn .dot {
      width: 8px; height: 8px; border-radius: 50%;
      background: var(--gray);
      box-shadow: 0 0 0 0 rgba(63,185,80,0.5);
      transition: background 0.2s;
    }
    .conn.ok .dot { background: var(--good); animation: pulse 1.6s infinite; }
    .conn.bad .dot { background: var(--bad); }
    @keyframes pulse {
      0%   { box-shadow: 0 0 0 0 rgba(63,185,80,0.5); }
      70%  { box-shadow: 0 0 0 8px rgba(63,185,80,0); }
      100% { box-shadow: 0 0 0 0 rgba(63,185,80,0); }
    }

    /* Status grid */
    .status-grid {
      display: grid;
      grid-template-columns: repeat(8, minmax(0, 1fr));
      gap: 10px;
    }
    @media (max-width: 1280px) { .status-grid { grid-template-columns: repeat(4, 1fr); } }
    @media (max-width: 720px)  { .status-grid { grid-template-columns: repeat(2, 1fr); } }
    .stat {
      position: relative;
      padding: 12px 14px;
      background: var(--bg-1);
      border: 1px solid var(--line);
      border-radius: 10px;
      overflow: hidden;
      transition: transform 0.15s, border-color 0.2s;
    }
    .stat:hover { transform: translateY(-1px); border-color: var(--line); }
    .stat .label {
      display: flex; align-items: center; gap: 6px;
      font-size: 11px; letter-spacing: 0.06em; text-transform: uppercase;
      color: var(--fg-2);
    }
    .stat .label .ico {
      display: inline-flex; align-items: center; justify-content: center;
      width: 16px; height: 16px; border-radius: 4px;
      background: rgba(255,255,255,0.04);
      font-size: 11px;
    }
    .stat .value {
      font-size: 26px; font-weight: 600; color: var(--fg-0);
      margin-top: 6px; font-variant-numeric: tabular-nums;
    }
    .stat .sub { font-size: 11px; color: var(--fg-3); margin-top: 2px; }
    .stat.zero .value { color: var(--fg-3); }
    .stat::before {
      content: ""; position: absolute; left: 0; top: 0; bottom: 0;
      width: 3px; background: var(--c, var(--accent));
    }

    /* Main panels */
    .main {
      display: grid;
      grid-template-columns: minmax(0, 1.4fr) minmax(360px, 1fr);
      gap: 14px;
      min-height: 0;
    }
    @media (max-width: 1100px) { .main { grid-template-columns: 1fr; } }
    .panel {
      background: var(--bg-1);
      border: 1px solid var(--line);
      border-radius: 12px;
      box-shadow: var(--shadow);
      display: flex; flex-direction: column;
      min-height: 0;
    }
    .panel-header {
      display: flex; align-items: center; gap: 10px;
      padding: 10px 14px;
      border-bottom: 1px solid var(--line);
    }
    .panel-header h3 {
      margin: 0; font-size: 13px; font-weight: 600; color: var(--fg-0);
      letter-spacing: 0.02em;
    }
    .panel-header .actions { margin-left: auto; display: flex; gap: 6px; }
    .panel-header input[type="search"], .panel-header select {
      background: var(--bg-2);
      border: 1px solid var(--line);
      color: var(--fg-1);
      border-radius: 6px;
      padding: 5px 8px;
      font-size: 12px;
      outline: none;
    }
    .panel-header input[type="search"] { width: 180px; }
    .panel-header input[type="search"]:focus, .panel-header select:focus { border-color: var(--accent); }
    .panel-body { flex: 1; overflow: auto; min-height: 0; }

    /* Issue table */
    table.issues { width: 100%; border-collapse: collapse; font-size: 12.5px; }
    table.issues th, table.issues td {
      padding: 9px 12px; text-align: left; border-bottom: 1px solid var(--line-soft);
      white-space: nowrap;
    }
    table.issues th {
      position: sticky; top: 0; z-index: 1;
      background: var(--bg-1); color: var(--fg-2); font-weight: 500;
      font-size: 11px; letter-spacing: 0.05em; text-transform: uppercase;
    }
    table.issues tr { cursor: pointer; transition: background 0.1s; }
    table.issues tr:hover { background: rgba(88,166,255,0.05); }
    table.issues tr.selected { background: rgba(88,166,255,0.10); }
    table.issues td.identifier { color: var(--fg-0); font-weight: 500; }
    table.issues td.branch, table.issues td.workspace { color: var(--fg-2); }
    table.issues td.workspace { max-width: 220px; overflow: hidden; text-overflow: ellipsis; }
    table.issues td.num { text-align: right; font-variant-numeric: tabular-nums; }
    .pill {
      display: inline-flex; align-items: center; gap: 6px;
      padding: 2px 8px; border-radius: 999px;
      font-size: 11px; font-weight: 500;
      background: color-mix(in srgb, var(--c) 14%, transparent);
      color: var(--c);
      border: 1px solid color-mix(in srgb, var(--c) 35%, transparent);
    }
    .pill .ico { font-size: 10px; }

    /* Detail panel */
    .detail { padding: 14px 16px; }
    .detail h2 {
      margin: 0 0 4px; font-size: 18px; color: var(--fg-0);
      display: flex; align-items: center; gap: 10px;
    }
    .detail .sub { color: var(--fg-2); font-size: 12px; }
    .detail .grid {
      display: grid; grid-template-columns: 110px 1fr; gap: 6px 14px;
      margin-top: 14px; font-size: 12.5px;
    }
    .detail .grid .k { color: var(--fg-2); }
    .detail .grid .v { color: var(--fg-1); word-break: break-all; }
    .detail .section-title {
      margin: 18px 0 8px; font-size: 11px; text-transform: uppercase;
      letter-spacing: 0.08em; color: var(--fg-3);
    }
    .events-mini {
      max-height: 240px; overflow: auto;
      border: 1px solid var(--line); border-radius: 8px;
      background: var(--bg-0);
    }
    .events-mini .row {
      display: grid; grid-template-columns: 72px 64px 1fr;
      gap: 10px; padding: 6px 10px; font-size: 12px;
      border-bottom: 1px solid var(--line-soft);
    }
    .events-mini .row:last-child { border-bottom: 0; }
    .events-mini .ts { color: var(--fg-3); font-variant-numeric: tabular-nums; }
    .events-mini .type {
      font-size: 10.5px; text-transform: uppercase; letter-spacing: 0.06em;
      color: var(--c, var(--fg-2));
    }
    .events-mini .row > span:last-child {
      white-space: pre-wrap; word-break: break-word;
    }
    .empty {
      padding: 28px 16px; text-align: center; color: var(--fg-3); font-size: 12px;
    }

    /* Live event feed */
    .feed {
      background: var(--bg-1);
      border: 1px solid var(--line);
      border-radius: 12px;
      box-shadow: var(--shadow);
      max-height: 260px;
      display: flex; flex-direction: column;
    }
    .feed .panel-body { font-family: "JetBrains Mono", "SF Mono", Menlo, monospace; font-size: 12px; }
    .feed .row {
      display: grid;
      grid-template-columns: 78px 60px 80px 1fr;
      gap: 10px; padding: 5px 14px;
      border-bottom: 1px solid var(--line-soft);
      align-items: baseline;
    }
    .feed .row:hover { background: rgba(88,166,255,0.05); }
    .feed .row .ts { color: var(--fg-3); font-variant-numeric: tabular-nums; }
    .feed .row .type { text-transform: uppercase; font-size: 10.5px; letter-spacing: 0.06em; color: var(--c, var(--fg-2)); }
    .feed .row .ident { color: var(--accent-2); }
    .feed .row .msg { color: var(--fg-1); white-space: pre-wrap; word-break: break-word; }

    /* Token + status distribution */
    .charts { display: grid; grid-template-columns: 2fr 1fr; gap: 14px; }
    @media (max-width: 1100px) { .charts { grid-template-columns: 1fr; } }
    .chart-card {
      background: var(--bg-1);
      border: 1px solid var(--line);
      border-radius: 12px;
      padding: 12px 16px;
      box-shadow: var(--shadow);
    }
    .chart-card h3 {
      margin: 0 0 8px; font-size: 12px; text-transform: uppercase;
      letter-spacing: 0.06em; color: var(--fg-2); font-weight: 500;
    }
    .dist-bar { display: flex; height: 10px; border-radius: 6px; overflow: hidden; background: var(--bg-2); }
    .dist-bar .seg { transition: flex 0.4s; }
    .dist-legend {
      display: grid; grid-template-columns: repeat(2, 1fr); gap: 4px 12px;
      margin-top: 10px; font-size: 11.5px; color: var(--fg-2);
    }
    .dist-legend .it { display: flex; align-items: center; gap: 6px; }
    .dist-legend .sw { width: 10px; height: 10px; border-radius: 3px; background: var(--c, var(--accent)); }
    .dist-legend .n { color: var(--fg-0); font-variant-numeric: tabular-nums; margin-left: auto; }

    /* Scrollbars */
    ::-webkit-scrollbar { width: 10px; height: 10px; }
    ::-webkit-scrollbar-track { background: transparent; }
    ::-webkit-scrollbar-thumb { background: var(--bg-3); border-radius: 5px; }
    ::-webkit-scrollbar-thumb:hover { background: #2a3445; }

    /* Utility */
    .small { font-size: 11.5px; color: var(--fg-2); }
    .muted { color: var(--fg-3); }
    .nowrap { white-space: nowrap; }
    .right { text-align: right; }
    .hidden { display: none !important; }
    .session-link {
      display: inline-flex; align-items: center; gap: 4px;
      margin-left: 6px; padding: 1px 7px;
      font-size: 11px; font-weight: 500;
      border-radius: 4px;
      background: rgba(88,166,255,0.10);
      color: var(--accent);
      border: 1px solid rgba(88,166,255,0.20);
      text-decoration: none;
      white-space: nowrap;
      transition: background 0.15s;
    }
    .session-link:hover { background: rgba(88,166,255,0.20); text-decoration: none; }
    .session-link::before { content: "▶"; font-size: 8px; }
  </style>
</head>
<body>
  <div class="app">
    <div class="header">
      <div class="brand">
        <div class="logo"></div>
        <div>ClawCodex <span class="muted">·</span> Orchestrator LiveView</div>
      </div>
      <div class="meta">
        <div class="kv">Workspace <b id="hdr-workspace" class="mono">…</b></div>
        <div class="kv">Daemon <b id="hdr-daemon">…</b></div>
        <div class="kv">Uptime <b id="hdr-uptime">—</b></div>
        <div class="kv">Pull Requests <b id="hdr-prs">0</b></div>
        <div class="kv">Event Count <b id="hdr-events">0</b></div>
      </div>
      <div id="conn" class="conn"><span class="dot"></span><span id="conn-text">connecting…</span></div>
    </div>

    <div class="status-grid" id="status-grid"></div>

    <div class="main">
      <div class="panel">
        <div class="panel-header">
          <h3>Issues</h3>
          <span class="small muted" id="issue-count">0</span>
          <div class="actions">
            <input id="filter" type="search" placeholder="Filter by id / branch…">
            <select id="status-filter">
              <option value="">All statuses</option>
            </select>
          </div>
        </div>
        <div class="panel-body">
          <table class="issues">
            <thead>
              <tr>
                <th>Status</th>
                <th>Identifier</th>
                <th>Branch</th>
                <th>PR</th>
                <th>Workspace</th>
                <th class="right">Attempts</th>
                <th class="right">Idle</th>
              </tr>
            </thead>
            <tbody id="issues-body">
              <tr><td colspan="7" class="empty">Loading…</td></tr>
            </tbody>
          </table>
        </div>
      </div>

      <div class="panel">
        <div class="panel-header">
          <h3>Detail</h3>
          <div class="actions">
            <span class="small muted" id="detail-sub">Select an issue to inspect</span>
          </div>
        </div>
        <div class="panel-body">
          <div id="detail" class="detail">
            <div class="empty">Click an issue on the left to see details, branch, commit, PR, and recent events.</div>
          </div>
        </div>
      </div>
    </div>

    <div class="charts">
      <div class="chart-card">
        <h3>Event Type Distribution</h3>
        <div class="dist-bar" id="dist-bar"></div>
        <div class="dist-legend" id="dist-legend"></div>
      </div>
      <div class="chart-card">
        <h3>Token Activity</h3>
        <div class="small" id="tok-summary" style="color:var(--fg-1)">
          <div>Active sessions: <b id="tok-active">0</b></div>
          <div>Turns: <b id="tok-turns">0</b> | Tools: <b id="tok-events">0</b></div>
        </div>
      </div>
    </div>

    <div class="feed">
      <div class="panel-header">
        <h3>Live Event Feed</h3>
        <span class="small muted" id="feed-count">0 events</span>
        <div class="actions">
          <select id="feed-type">
            <option value="">All event types</option>
            <option value="tool_call">Tool Call</option>
            <option value="tool_result">Tool Result</option>
            <option value="agent_text">Agent Text</option>
          </select>
          <button id="feed-clear" style="background:var(--bg-2);border:1px solid var(--line);color:var(--fg-1);border-radius:6px;padding:4px 8px;cursor:pointer;">Clear</button>
        </div>
      </div>
      <div class="panel-body" id="feed-body">
        <div class="empty">Waiting for events…</div>
      </div>
    </div>
  </div>

  <script>
    "use strict";

    // ---- Constants ---------------------------------------------------------
    const STATUS_META = __STATUS_META__;
    const STATUSES = Object.keys(STATUS_META);
    const ACTIVE = new Set(STATUSES.filter(s => STATUS_META[s].group === "active"));
    const TERMINAL = new Set(STATUSES.filter(s => STATUS_META[s].group === "terminal"));

    const EVENT_TYPE_META = {
      tool_call:      { color: "#a371f7", label: "Tool Call" },
      tool_result:    { color: "#3fb950", label: "Tool Result" },
      agent_text:     { color: "#79c0ff", label: "Agent Text" },
    };
    const MAX_FEED_ROWS = 500;

    // ---- State -------------------------------------------------------------
    const state = {
      snapshot: null,
      selectedIssueId: null,
      _lastDetailSig: "",   // signature of last rendered detail panel
      _feedClearedTs: 0,    // Unix ts of last "Clear" click; events older than this are suppressed
      filter: "",
      statusFilter: "",
      feedTypeFilter: "",
      feed: [],            // newest first
      offsets: {},         // last seen file offsets (per issue_id)
    };

    // ---- DOM refs ----------------------------------------------------------
    const $ = (id) => document.getElementById(id);
    const el = {
      conn: $("conn"), connText: $("conn-text"),
      hdrWorkspace: $("hdr-workspace"), hdrDaemon: $("hdr-daemon"),
      hdrUptime: $("hdr-uptime"), hdrPrs: $("hdr-prs"), hdrEvents: $("hdr-events"),
      statusGrid: $("status-grid"),
      issueCount: $("issue-count"),
      issuesBody: $("issues-body"),
      filter: $("filter"),
      statusFilter: $("status-filter"),
      feedBody: $("feed-body"), feedCount: $("feed-count"),
      feedType: $("feed-type"), feedClear: $("feed-clear"),
      detail: $("detail"), detailSub: $("detail-sub"),
      distBar: $("dist-bar"), distLegend: $("dist-legend"),
      tokActive: $("tok-active"), tokEvents: $("tok-events"),
      tokTurns: $("tok-turns"),
    };

    // ---- Utilities ---------------------------------------------------------
    function fmtAge(seconds) {
      if (seconds == null) return "—";
      if (seconds < 60) return seconds + "s";
      if (seconds < 3600) return Math.floor(seconds/60) + "m " + (seconds%60) + "s";
      const h = Math.floor(seconds/3600);
      const m = Math.floor((seconds%3600)/60);
      return h + "h " + m + "m";
    }
    function fmtUptime(seconds) {
      if (!seconds) return "—";
      if (seconds < 60) return seconds + "s";
      if (seconds < 3600) return Math.floor(seconds/60) + "m";
      const h = Math.floor(seconds/3600);
      const m = Math.floor((seconds%3600)/60);
      return h + "h " + m + "m";
    }
    function fmtNumber(n) {
      if (n == null) return "0";
      if (n < 1000) return String(n);
      if (n < 1_000_000) return (n/1000).toFixed(n < 10000 ? 1 : 0) + "k";
      return (n/1_000_000).toFixed(1) + "M";
    }
    function shortSha(sha) { return sha ? sha.slice(0, 7) : ""; }
    function escapeHtml(s) {
      return String(s == null ? "" : s)
        .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
    }
    function truncate(s, n) {
      s = s == null ? "" : String(s);
      return s.length > n ? s.slice(0, n - 1) + "…" : s;
    }

    // ---- Event body helpers (shared by renderFeed + renderDetail) --------
    function toolCallBody(ev) {
      const name = escapeHtml(ev.tool_name || "?");
      const p = ev.params;
      let detail = "";
      if (p && typeof p === "object" && !Array.isArray(p)) {
        // Tool-specific extraction for readability
        const cmd = p.command || p.cmd;
        const path = p.file_path || p.path || p.pathname;
        const pattern = p.pattern || p.query || p.regex;
        const url = p.url;
        if (cmd) detail = truncate(cmd, 120);
        else if (path) detail = truncate(path, 100);
        else if (pattern) detail = truncate(pattern, 80);
        else if (url) detail = truncate(url, 100);
        else {
          const vals = Object.values(p).filter(v => v != null);
          if (vals.length > 0) detail = truncate(String(vals[0]), 100);
        }
      } else if (typeof p === "string") {
        detail = truncate(p, 120);
      }
      return `<b>${name}</b>` + (detail ? ` <span class="muted">${escapeHtml(detail)}</span>` : "");
    }
    function toolResultBody(ev) {
      const name = escapeHtml(ev.tool_name || "?");
      const err = ev.is_error ? ' <span style="color:var(--bad)">[error]</span>' : "";
      let detail = "";
      const c = ev.result_content;
      if (c) {
        detail = typeof c === "string" ? c : (Array.isArray(c) ? c.map(b => (b && b.text) || "").join("") : JSON.stringify(c));
        detail = truncate(detail, 200);
      }
      return `<b>${name}</b>${err}` + (detail ? ` <span class="muted">${escapeHtml(detail)}</span>` : "");
    }
    function eventBody(ev) {
      const t = ev.type || "?";
      if (t === "tool_call") return toolCallBody(ev);
      if (t === "tool_result") return toolResultBody(ev);
      if (t === "agent_text") return `<span>${escapeHtml(truncate(ev.content || "", 160))}</span>`;
      return `<span class="mono">${escapeHtml(truncate(JSON.stringify(ev), 140))}</span>`;
    }

    function pillFor(status) {
      const m = STATUS_META[status] || STATUS_META.pending;
      return `<span class="pill" style="--c:${m.color}"><span class="ico">${m.icon}</span>${m.label}</span>`;
    }

    // ---- Rendering ---------------------------------------------------------
    function renderStatusGrid(byStatus) {
      const html = STATUSES.map(s => {
        const m = STATUS_META[s];
        const n = byStatus[s] || 0;
        const isZero = n === 0 ? " zero" : "";
        const groupLabel = m.group === "active" ? "active" : "terminal";
        return `
          <div class="stat${isZero}" style="--c:${m.color}">
            <div class="label"><span class="ico" style="color:${m.color}">${m.icon}</span>${m.label}<span class="muted" style="margin-left:auto">${groupLabel}</span></div>
            <div class="value">${n}</div>
            <div class="sub">${STATUSES.indexOf(s) >= 0 ? "issues in state" : ""}</div>
          </div>`;
      }).join("");
      el.statusGrid.innerHTML = html;
    }

    function renderIssues(issues) {
      const f = state.filter.trim().toLowerCase();
      const sf = state.statusFilter;
      const filtered = issues.filter(i => {
        if (sf && i.status !== sf) return false;
        if (f) {
          const hay = [i.identifier, i.branch_name, i.workspace_path].filter(Boolean).join(" ").toLowerCase();
          if (!hay.includes(f)) return false;
        }
        return true;
      });
      el.issueCount.textContent = filtered.length + " / " + issues.length;
      if (filtered.length === 0) {
        el.issuesBody.innerHTML = `<tr><td colspan="7" class="empty">No issues match the current filter.</td></tr>`;
        return;
      }
      const rows = filtered.map(i => {
        const pr = i.pr_number
          ? `<a href="${escapeHtml(i.pr_url || "#")}" target="_blank" rel="noopener">#${escapeHtml(i.pr_number)}</a>`
          : `<span class="muted">—</span>`;
        const branch = i.branch_name
          ? `<span class="mono">${escapeHtml(truncate(i.branch_name, 28))}</span>`
          : `<span class="muted">—</span>`;
        const ws = i.workspace_short
          ? `<span class="mono" title="${escapeHtml(i.workspace_path || "")}">${escapeHtml(truncate(i.workspace_short, 30))}</span>`
          : `<span class="muted">—</span>`;
        const sel = i.issue_id === state.selectedIssueId ? " selected" : "";
        return `
          <tr class="issue-row${sel}" data-id="${escapeHtml(i.issue_id)}">
            <td>${pillFor(i.status)}</td>
            <td class="identifier">${escapeHtml(i.identifier)}</td>
            <td class="branch">${branch}</td>
            <td>${pr}</td>
            <td class="workspace">${ws}</td>
            <td class="num">${i.attempt_count || 0}</td>
            <td class="num">${fmtAge(i.idle_seconds)}</td>
          </tr>`;
      }).join("");
      el.issuesBody.innerHTML = rows;
      el.issuesBody.querySelectorAll(".issue-row").forEach(tr => {
        tr.addEventListener("click", () => selectIssue(tr.getAttribute("data-id")));
      });
    }

    function renderDistribution(byType) {
      const entries = Object.entries(byType || {});
      const total = entries.reduce((a, [, n]) => a + n, 0);
      if (total === 0) {
        el.distBar.innerHTML = `<div class="seg" style="flex:1;background:var(--bg-3)"></div>`;
        el.distLegend.innerHTML = `<div class="muted small">No events yet</div>`;
        return;
      }
      el.distBar.innerHTML = entries.map(([t, n]) => {
        const m = EVENT_TYPE_META[t] || { color: "#8b949e", label: t };
        return `<div class="seg" style="flex:${n};background:${m.color}" title="${m.label}: ${n}"></div>`;
      }).join("");
      el.distLegend.innerHTML = entries.map(([t, n]) => {
        const m = EVENT_TYPE_META[t] || { color: "#8b949e", label: t };
        const pct = ((n / total) * 100).toFixed(1);
        return `<div class="it"><span class="sw" style="--c:${m.color};background:${m.color}"></span>${m.label}<span class="n">${fmtNumber(n)} (${pct}%)</span></div>`;
      }).join("");
    }

    function renderHeader(snap) {
      el.hdrWorkspace.textContent = snap.workspace || "—";
      el.hdrWorkspace.title = snap.workspace || "";
      const m = snap.metadata || {};
      if (m.found) {
        el.hdrDaemon.innerHTML = m.alive
          ? `<span style="color:var(--good)">●</span> running (PID ${m.pid || "?"})`
          : `<span style="color:var(--bad)">●</span> stopped`;
        el.hdrDaemon.title = m.metadata_path || "";
        el.hdrUptime.textContent = m.alive ? fmtUptime(m.uptime_seconds) : "—";
      } else {
        el.hdrDaemon.innerHTML = `<span class="muted">not detected</span>`;
        el.hdrUptime.textContent = "—";
      }
      el.hdrPrs.textContent = (snap.issues && snap.issues.totals && snap.issues.totals.prs) || 0;
      el.hdrEvents.textContent = (snap.events && snap.events.total) || 0;
      // Token Activity: active sessions / turns / tools
      const ta = snap.token_activity || {};
      el.tokActive.textContent = ta.active_sessions || 0;
      el.tokTurns.textContent = ta.total_turns || 0;
      el.tokEvents.textContent = ta.total_tools || 0;
    }

    function renderDetail(issueId) {
      if (!state.snapshot) return;
      const issues = state.snapshot.issues.issues || [];
      const issue = issues.find(i => i.issue_id === issueId);
      if (!issue) {
        el.detail.innerHTML = `<div class="empty">Issue not found.</div>`;
        el.detailSub.textContent = "—";
        return;
      }
      const m = STATUS_META[issue.status] || STATUS_META.pending;
      const events = (state.snapshot.events && state.snapshot.events.recent || [])
        .filter(e => e.issue_id === issueId)
        .slice(0, 20);
      el.detailSub.innerHTML = `${pillFor(issue.status)} <span class="muted small">· ${issue.workspace_strategy || "—"} strategy</span>`;
      const rows = [
        ["Identifier",   `<b>${escapeHtml(issue.identifier)}</b>`],
        ["Issue ID",     `<span class="mono">${escapeHtml(issue.issue_id)}</span>`],
        ["Status",       pillFor(issue.status)],
        ["Branch",       issue.branch_name ? `<span class="mono">${escapeHtml(issue.branch_name)}</span>` : `<span class="muted">—</span>`],
        ["Base branch",  `<span class="mono">${escapeHtml(issue.base_branch || "main")}</span>`],
        ["Commit",       issue.commit_sha ? `<span class="mono">${escapeHtml(shortSha(issue.commit_sha))}</span> <span class="muted">${escapeHtml(issue.commit_sha)}</span>` : `<span class="muted">—</span>`],
        ["Pull request", issue.pr_number ? `<a href="${escapeHtml(issue.pr_url || "#")}" target="_blank" rel="noopener">#${escapeHtml(issue.pr_number)}</a>` : `<span class="muted">—</span>`],
        ["Workspace",    issue.workspace_path ? `<span class="mono" title="${escapeHtml(issue.workspace_path)}">${escapeHtml(issue.workspace_path)}</span>` : `<span class="muted">—</span>`],
        ["Sequence",     issue.sequence_index != null ? `#${issue.sequence_index}` : `<span class="muted">—</span>`],
        ["Intent",       `<span class="mono">${escapeHtml(issue.intent || "none")}</span>`],
        ["Attempts",     String(issue.attempt_count || 0)],
        ["Retries",      String(issue.retry_count || 0)],
        ["Verification", issue.verification_status ? escapeHtml(issue.verification_status) : `<span class="muted">—</span>`],
        ["Clarification",issue.clarification_status ? escapeHtml(issue.clarification_status) : `<span class="muted">—</span>`],
        ["Created",      issue.created_at ? new Date(issue.created_at * 1000).toLocaleString() : `<span class="muted">—</span>`],
        ["Updated",      issue.updated_at ? new Date(issue.updated_at * 1000).toLocaleString() : `<span class="muted">—</span>`],
        ["Idle",         fmtAge(issue.idle_seconds)],
        ["Age",          fmtAge(issue.age_seconds)],
      ];
      const eventsHtml = events.length === 0
        ? `<div class="empty">No recent events for this issue.</div>`
        : events.map(e => {
            const ev = e.event || {};
            const t = ev.type || "?";
            const m2 = EVENT_TYPE_META[t] || { color: "#8b949e", label: t.toUpperCase() };
            const body = eventBody(ev);
            return `<div class="row" style="--c:${m2.color}"><span class="ts">${escapeHtml(e.timestamp || "")}</span><span class="type" style="color:${m2.color}">${m2.label}</span><span>${body}</span></div>`;
          }).join("");

      el.detail.innerHTML = `
        <h2>${escapeHtml(issue.identifier)} ${pillFor(issue.status)}</h2>
        <div class="sub mono">${escapeHtml(issue.issue_id)}</div>
        <div class="grid">
          ${rows.map(([k, v]) => `<div class="k">${k}</div><div class="v">${v}</div>`).join("")}
        </div>
        <div class="section-title">Recent Events (${events.length})</div>
        <div class="events-mini">${eventsHtml}</div>
      `;
    }

    function renderFeed() {
      const f = state.feedTypeFilter;
      const rows = state.feed.filter(e => !f || (e.event && e.event.type === f));
      el.feedCount.textContent = rows.length + " events";
      if (rows.length === 0) {
        el.feedBody.innerHTML = `<div class="empty">No events match the current filter.</div>`;
        return;
      }
      el.feedBody.innerHTML = rows.slice(0, MAX_FEED_ROWS).map(e => {
        const ev = e.event || {};
        const t = ev.type || "?";
        const m = EVENT_TYPE_META[t] || { color: "#8b949e", label: t.toUpperCase() };
        const body = eventBody(ev);
        return `<div class="row" style="--c:${m.color}"><span class="ts">${escapeHtml(e.timestamp || "")}</span><span class="type" style="color:${m.color}">${m.label}</span><span class="ident">${escapeHtml(identifierFor(e.issue_id))}</span><span class="msg">${body}</span></div>`;
      }).join("");
    }

    function identifierFor(issueId) {
      if (!state.snapshot) return issueId;
      const issue = (state.snapshot.issues.issues || []).find(i => i.issue_id === issueId);
      return issue ? issue.identifier : issueId;
    }

    function renderStatusFilter() {
      if (!state.snapshot) return;
      const counts = state.snapshot.issues.by_status || {};
      const current = state.statusFilter;
      const opts = [`<option value="">All statuses</option>`].concat(
        STATUSES.map(s => `<option value="${s}" ${s === current ? "selected" : ""}>${STATUS_META[s].label} (${counts[s] || 0})</option>`)
      );
      el.statusFilter.innerHTML = opts.join("");
    }

    function renderAll() {
      if (!state.snapshot) return;
      renderHeader(state.snapshot);
      renderStatusGrid(state.snapshot.issues.by_status || {});
      renderStatusFilter();
      renderIssues(state.snapshot.issues.issues || []);
      renderDistribution((state.snapshot.events && state.snapshot.events.by_type) || {});
      // Only re-render detail panel when its data actually changed.
      // This prevents the "Recent Events" list from being rebuilt every 0.5s,
      // which would reset scroll position and make it unreadable.
      if (state.selectedIssueId) {
        const issue = (state.snapshot.issues.issues || []).find(i => i.issue_id === state.selectedIssueId);
        if (issue) {
          const evtCount = (state.snapshot.events && state.snapshot.events.recent || [])
            .filter(e => e.issue_id === state.selectedIssueId).length;
          const sig = state.selectedIssueId + '|' + issue.status + '|' + issue.updated_at + '|' + evtCount;
          if (sig !== state._lastDetailSig) {
            renderDetail(state.selectedIssueId);
            state._lastDetailSig = sig;
          }
        }
      }
      renderFeed();
    }

    function applySnapshot(snap) {
      state.snapshot = snap;
      if (!snap.events) snap.events = { by_type: {}, total: 0, recent: [] };

      // Normalize events.recent in-place (EventTailerManager format → renderFeed/renderDetail format)
      if (snap.events.recent) {
        snap.events.recent = snap.events.recent.map(function(e) {
          // Already normalized (e.g. from a previous applySnapshot pass)
          if (e.event) return e;
          return {
            event: {
              type: e.event_type || '?',
              tool_name: (e.data && e.data.tool) || (e.data && e.data.tool_name) || '?',
              approved: e.data && e.data.approved,
              turn: e.data && e.data.turn,
              deny_reason: e.data && e.data.deny_reason,
              is_error: e.data && e.data.is_error,
              params: e.data && e.data.params,
              result_content: e.data && e.data.result_content,
              content: e.data && e.data.content,
            },
            timestamp: e.ts ? new Date(e.ts * 1000).toLocaleTimeString("en-GB", { hour12: false }) : "",
            issue_id: e.issue_id,
            ts: e.ts,
          };
        });
      }

      // Incremental feed update: de-duplicate by ts+issue_id, respect Clear timestamp
      if (snap.events.recent && snap.events.recent.length > 0) {
        const clearedTs = state._feedClearedTs || 0;
        const existingKeys = new Set(state.feed.map(e => e.ts + '|' + e.issue_id));
        const newEvents = snap.events.recent
          .filter(e => !existingKeys.has(e.ts + '|' + e.issue_id))
          .filter(e => !clearedTs || e.ts > clearedTs)
          .reverse();
        newEvents.forEach(e => state.feed.unshift(e));
        if (state.feed.length > MAX_FEED_ROWS * 2) state.feed.length = MAX_FEED_ROWS * 2;
      }

      renderAll();
    }

    function selectIssue(issueId) {
      state.selectedIssueId = state.selectedIssueId === issueId ? null : issueId;
      state._lastDetailSig = "";  // force re-render on manual selection
      renderIssues(state.snapshot.issues.issues || []);
      if (state.selectedIssueId) renderDetail(state.selectedIssueId);
      else {
        el.detail.innerHTML = `<div class="empty">Click an issue on the left to see details, branch, commit, PR, and recent events.</div>`;
        el.detailSub.textContent = "Select an issue to inspect";
      }
    }

    // ---- Event handling ----------------------------------------------------
    function pushEvent(evt) {
      state.feed.unshift(evt);
      if (state.feed.length > MAX_FEED_ROWS * 2) state.feed.length = MAX_FEED_ROWS * 2;
      // Increment by_type counter and update distribution live.
      if (state.snapshot && evt.event && evt.event.type) {
        if (!state.snapshot.events) state.snapshot.events = { by_type: {}, total: 0, recent: [] };
        const bt = state.snapshot.events.by_type || (state.snapshot.events.by_type = {});
        bt[evt.event.type] = (bt[evt.event.type] || 0) + 1;
        state.snapshot.events.total = (state.snapshot.events.total || 0) + 1;
        renderDistribution(bt);
        el.hdrEvents.textContent = state.snapshot.events.total;
      }
      renderFeed();
    }

    // ---- SSE ---------------------------------------------------------------
    let es = null;
    function connect() {
      if (es) { try { es.close(); } catch (e) {} }
      es = new EventSource("/events");
      es.onopen = () => {
        el.conn.classList.remove("bad"); el.conn.classList.add("ok");
        el.connText.textContent = "connected";
      };
      es.onerror = () => {
        el.conn.classList.remove("ok"); el.conn.classList.add("bad");
        el.connText.textContent = "disconnected — retrying…";
      };
      es.onmessage = (msg) => {
        let data;
        try { data = JSON.parse(msg.data); } catch (e) { return; }
        if (data.type === "snapshot") {
          applySnapshot(data);
        } else if (data.type === "event") {
          pushEvent(data);
        }
      };
    }

    // ---- Wire up -----------------------------------------------------------
    el.filter.addEventListener("input", (e) => {
      state.filter = e.target.value;
      if (state.snapshot) renderIssues(state.snapshot.issues.issues || []);
    });
    el.statusFilter.addEventListener("change", (e) => {
      state.statusFilter = e.target.value;
      if (state.snapshot) renderIssues(state.snapshot.issues.issues || []);
    });
    el.feedType.addEventListener("change", (e) => {
      state.feedTypeFilter = e.target.value;
      renderFeed();
    });
    el.feedClear.addEventListener("click", () => {
      state._feedClearedTs = Date.now() / 1000;
      state.feed = [];
      renderFeed();
    });

    connect();
    // Periodic refresh to keep header idle/uptime counters ticking even when
    // the server is idle.
    setInterval(() => {
      if (state.snapshot) renderHeader(state.snapshot);
    }, 1000);
  </script>
</body>
</html>"""


CHAT_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Orchestratord Chat</title>
  <style>
    :root {
      --bg-0: #0b0f17; --bg-1: #11161f; --bg-2: #161c26; --bg-3: #1d2532;
      --line: #232c3a; --line-soft: #1a212c; --fg-0: #e6edf3; --fg-1: #c9d1d9;
      --fg-2: #8b949e; --fg-3: #6e7681; --accent: #58a6ff; --good: #3fb950;
      --warn: #d29922; --bad: #f85149; --purple: #a371f7;
    }
    * { box-sizing: border-box; margin: 0; padding: 0; }
    html, body {
      height: 100%; background: var(--bg-0); color: var(--fg-1);
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "Helvetica Neue",
                   "PingFang SC", "Microsoft YaHei", Arial, sans-serif;
      font-size: 13px; line-height: 1.5;
    }
    .app { display: flex; height: 100vh; }
    /* Sidebar */
    .sidebar {
      width: 260px; min-width: 260px; background: var(--bg-1);
      border-right: 1px solid var(--line); display: flex; flex-direction: column;
    }
    .sidebar-header {
      padding: 14px; border-bottom: 1px solid var(--line);
      font-weight: 600; font-size: 14px; color: var(--fg-0);
    }
    .run-list { flex: 1; overflow-y: auto; }
    .run-item {
      padding: 10px 14px; cursor: pointer; border-bottom: 1px solid var(--line-soft);
      transition: background 0.15s;
    }
    .run-item:hover { background: var(--bg-2); }
    .run-item.active { background: var(--bg-3); border-left: 3px solid var(--accent); }
    .run-item .run-id { font-weight: 500; color: var(--fg-0); font-size: 12px; }
    .run-item .run-meta { font-size: 11px; color: var(--fg-3); margin-top: 2px; }
    .status-dot {
      display: inline-block; width: 6px; height: 6px; border-radius: 50%; margin-right: 4px;
    }
    .status-dot.running { background: var(--accent); }
    .status-dot.completed { background: var(--good); }
    .status-dot.failed { background: var(--bad); }
    /* Main chat area */
    .main {
      flex: 1; display: flex; flex-direction: column; min-width: 0;
    }
    .chat-header {
      padding: 12px 18px; border-bottom: 1px solid var(--line);
      display: flex; align-items: center; gap: 12px; background: var(--bg-1);
    }
    .chat-header .title { font-weight: 600; color: var(--fg-0); flex: 1; }
    .chat-header button {
      padding: 4px 12px; border-radius: 6px; border: 1px solid var(--line);
      background: var(--bg-2); color: var(--fg-1); cursor: pointer; font-size: 12px;
    }
    .chat-header button:hover { background: var(--bg-3); }
    .chat-header button.danger { color: var(--bad); border-color: var(--bad); }
    .messages {
      flex: 1; overflow-y: auto; padding: 16px;
    }
    .msg { margin-bottom: 14px; max-width: 80%; }
    .msg.user { margin-left: auto; }
    .msg.agent { margin-right: auto; }
    .msg .bubble {
      padding: 10px 14px; border-radius: 12px; line-height: 1.5;
      white-space: pre-wrap; word-break: break-word;
    }
    .msg.user .bubble { background: var(--accent); color: #fff; }
    .msg.agent .bubble { background: var(--bg-2); border: 1px solid var(--line); }
    .msg.agent .bubble.streaming { border-color: var(--accent); }
    .tool-card {
      margin: 8px 0; padding: 10px 14px; background: var(--bg-1);
      border: 1px solid var(--line); border-radius: 8px; font-size: 12px;
    }
    .tool-card .tool-name { font-weight: 600; color: var(--purple); }
    .tool-card .tool-result { color: var(--fg-2); margin-top: 4px; }
    .tool-card .tool-result.truncated { color: var(--warn); }
    .input-bar {
      padding: 12px 18px; border-top: 1px solid var(--line);
      display: flex; gap: 8px; background: var(--bg-1);
    }
    .input-bar input {
      flex: 1; padding: 8px 12px; border-radius: 8px; border: 1px solid var(--line);
      background: var(--bg-0); color: var(--fg-0); font-size: 13px; outline: none;
    }
    .input-bar input:focus { border-color: var(--accent); }
    .input-bar button {
      padding: 8px 16px; border-radius: 8px; border: none;
      background: var(--accent); color: #fff; cursor: pointer; font-size: 13px;
    }
    .input-bar button:disabled { opacity: 0.5; cursor: not-allowed; }
    .status-chip {
      display: inline-block; padding: 2px 8px; border-radius: 999px;
      font-size: 11px; font-weight: 500;
    }
    .status-chip.paused { background: var(--warn); color: #000; }
    .status-chip.running { background: var(--accent); color: #fff; }
    .empty-state {
      display: flex; align-items: center; justify-content: center;
      height: 100%; color: var(--fg-3); font-size: 14px;
    }
  </style>
</head>
<body>
<div class="app">
  <div class="sidebar">
    <div class="sidebar-header">Active Runs</div>
    <div class="run-list" id="run-list"></div>
  </div>
  <div class="main">
    <div class="chat-header" id="chat-header">
      <span class="title">Select a run to start chatting</span>
      <span id="status-chip"></span>
      <button onclick="pauseRun()" id="btn-pause" disabled>Pause</button>
      <button onclick="resumeRun()" id="btn-resume" disabled>Resume</button>
      <button class="danger" onclick="stopRun()" id="btn-stop" disabled>Stop</button>
    </div>
    <div class="messages" id="messages">
      <div class="empty-state">Select a run from the sidebar to view the conversation</div>
    </div>
    <div class="input-bar">
      <input type="text" id="msg-input" placeholder="Type a message (Enter to send)..." disabled
             onkeydown="if(event.key==='Enter')sendMessage()">
      <button id="btn-send" disabled onclick="sendMessage()">Send</button>
    </div>
  </div>
</div>
<script>
// ---- State ----
let currentRunId = null;
let currentStreamingBubble = null;
let eventSource = null;
let pendingMessages = {};

// ---- Run list ----
async function loadRuns() {
  const resp = await fetch('/api/runs');
  const data = await resp.json();
  const list = document.getElementById('run-list');
  list.innerHTML = data.runs.map(r => {
    const dotClass = r.status === 'running' ? 'running' :
                     r.status === 'completed' ? 'completed' : 'failed';
    const active = r.run_id === currentRunId ? ' active' : '';
    return `<div class="run-item${active}" onclick="selectRun('${r.run_id}')">
      <div class="run-id"><span class="status-dot ${dotClass}"></span>${r.issue_id}</div>
      <div class="run-meta">${r.run_id.slice(0,8)}... · ${r.status}</div>
    </div>`;
  }).join('');
}

// ---- Run selection ----
function selectRun(runId) {
  if (eventSource) { eventSource.close(); eventSource = null; }
  currentRunId = runId;
  document.getElementById('messages').innerHTML = '';
  currentStreamingBubble = null;
  document.getElementById('msg-input').disabled = false;
  document.getElementById('btn-send').disabled = false;
  document.getElementById('btn-pause').disabled = false;
  document.getElementById('btn-resume').disabled = false;
  document.getElementById('btn-stop').disabled = false;
  document.getElementById('chat-header').querySelector('.title').textContent = runId;
  loadRuns();
  connectSSE(runId);
}

// ---- SSE ----
function connectSSE(runId) {
  eventSource = new EventSource('/api/runs/' + runId + '/events');
  eventSource.onmessage = function(e) {
    const msg = JSON.parse(e.data);
    if (msg.type === 'history') {
      renderHistory(msg.messages);
    } else if (msg.type === 'boundary') {
      // Switch to streaming mode
    } else if (msg.type === 'frame') {
      renderFrame(msg.frame);
    } else if (msg.type === 'RunEnded') {
      document.getElementById('msg-input').disabled = true;
      document.getElementById('btn-send').disabled = true;
      document.getElementById('status-chip').innerHTML = '<span class="status-chip">ended</span>';
    }
  };
  eventSource.onerror = function() {
    setTimeout(() => { if (currentRunId) connectSSE(currentRunId); }, 2000);
  };
}

// ---- Render ----
function renderHistory(messages) {
  const container = document.getElementById('messages');
  container.innerHTML = '';
  messages.forEach(m => {
    const role = m.role;
    if (role === 'user' || role === 'human') {
      appendMessage('user', formatContent(m.content));
    } else if (role === 'assistant') {
      appendMessage('agent', formatContent(m.content));
    }
  });
}

function formatContent(content) {
  if (typeof content === 'string') return content;
  if (Array.isArray(content)) {
    return content.map(b => {
      if (b.type === 'text') return b.text || '';
      if (b.type === 'tool_use') return '[Tool: ' + (b.name || '?') + ']';
      if (b.type === 'tool_result') return '[Result]';
      return '';
    }).join('');
  }
  return JSON.stringify(content);
}

function renderFrame(frame) {
  const type = frame.type;
  if (type === 'TextDelta') {
    const text = frame.data?.content || '';
    if (!currentStreamingBubble) {
      currentStreamingBubble = appendMessage('agent', '');
      currentStreamingBubble.querySelector('.bubble').classList.add('streaming');
    }
    const bubble = currentStreamingBubble.querySelector('.bubble');
    bubble.textContent += text;
    scrollToBottom();
  } else if (type === 'ToolCallEvent') {
    const toolName = frame.data?.tool_name || '?';
    appendToolCard(toolName, null);
  } else if (type === 'ToolResultEvent') {
    const result = frame.data?.result || {};
    const output = result.output || '';
    const truncated = result.truncated;
    updateLastToolCard(output, truncated);
  } else if (type === 'TurnComplete') {
    if (currentStreamingBubble) {
      currentStreamingBubble.querySelector('.bubble').classList.remove('streaming');
      currentStreamingBubble = null;
    }
  } else if (type === 'FollowupQueued') {
    // Mark pending message as delivered
  } else if (type === 'Paused') {
    document.getElementById('status-chip').innerHTML = '<span class="status-chip paused">paused</span>';
  } else if (type === 'Resumed') {
    document.getElementById('status-chip').innerHTML = '<span class="status-chip running">running</span>';
  }
}

function appendMessage(role, text) {
  const container = document.getElementById('messages');
  const div = document.createElement('div');
  div.className = 'msg ' + role;
  div.innerHTML = '<div class="bubble">' + escapeHtml(text) + '</div>';
  container.appendChild(div);
  scrollToBottom();
  return div;
}

function appendToolCard(toolName, result) {
  const container = document.getElementById('messages');
  const div = document.createElement('div');
  div.className = 'tool-card';
  div.innerHTML = '<div class="tool-name">' + escapeHtml(toolName) + '</div>' +
    (result ? '<div class="tool-result' + (result.length > 500 ? ' truncated' : '') + '">' +
     escapeHtml(result.slice(0, 500)) + (result.length > 500 ? '...' : '') + '</div>' : '');
  container.appendChild(div);
  scrollToBottom();
  return div;
}

function updateLastToolCard(output, truncated) {
  const cards = document.querySelectorAll('.tool-card');
  if (cards.length === 0) return;
  const last = cards[cards.length - 1];
  const resultDiv = last.querySelector('.tool-result');
  if (resultDiv) {
    resultDiv.textContent = output.slice(0, 500) + (truncated ? '...' : '');
    if (truncated) resultDiv.classList.add('truncated');
  } else if (output) {
    const div = document.createElement('div');
    div.className = 'tool-result' + (truncated ? ' truncated' : '');
    div.textContent = output.slice(0, 500) + (truncated ? '...' : '');
    last.appendChild(div);
  }
  scrollToBottom();
}

function escapeHtml(text) {
  const div = document.createElement('div');
  div.textContent = text;
  return div.innerHTML;
}

function scrollToBottom() {
  const container = document.getElementById('messages');
  container.scrollTop = container.scrollHeight;
}

// ---- Actions ----
async function sendMessage() {
  const input = document.getElementById('msg-input');
  const text = input.value.trim();
  if (!text || !currentRunId) return;
  input.value = '';
  appendMessage('user', text);
  const resp = await fetch('/api/runs/' + currentRunId + '/messages', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({text: text})
  });
}

async function pauseRun() {
  if (!currentRunId) return;
  await fetch('/api/runs/' + currentRunId + '/pause', {method: 'POST'});
}

async function resumeRun() {
  if (!currentRunId) return;
  await fetch('/api/runs/' + currentRunId + '/resume', {method: 'POST'});
}

async function stopRun() {
  if (!currentRunId) return;
  await fetch('/api/runs/' + currentRunId + '/stop', {method: 'POST'});
}

// ---- Init ----
loadRuns();
setInterval(loadRuns, 3000);
</script>
</body>
</html>"""


def _build_dashboard_html() -> str:
    """Inject the STATUS_META JSON into the HTML template."""
    return DASHBOARD_HTML.replace(
        "__STATUS_META__",
        json.dumps(STATUS_META, ensure_ascii=False),
    )


# ---------------------------------------------------------------------------
# HTTP server
# ---------------------------------------------------------------------------


class DashboardHandler(BaseHTTPRequestHandler):
    """HTTP handler serving the dashboard UI, JSON snapshots, and SSE events."""

    server_version = "ClawCodexDashboard/1.0"
    state: DashboardState  # set on the class by run()

    # Quieter logs — one line per request is too noisy for a polling UI.
    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        return

    def _send_json(self, payload: Any, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _send_text(self, text: str, content_type: str, status: int = 200) -> None:
        body = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path or "/"

        if path == "/":
            self._send_text(_build_dashboard_html(), "text/html; charset=utf-8")
            return

        if path == "/api/state":
            snap = self.state.refresh_snapshot(force=True)
            self._send_json(snap)
            return

        if path.startswith("/api/issue/"):
            issue_id = path[len("/api/issue/") :]
            snap = self.state.refresh_snapshot(force=True)
            for issue in snap["issues"]["issues"]:
                if issue["issue_id"] == issue_id:
                    self._send_json({"issue": issue})
                    return
            self._send_json({"error": "not found", "issue_id": issue_id}, status=404)
            return

        if path == "/api/health":
            self._send_json(
                {
                    "ok": True,
                    "workspace": str(self.state.workspace),
                    "ts": time.time(),
                }
            )
            return

        if path == "/events":
            self._stream_events()
            return

        if path == "/chat":
            self._send_text(CHAT_HTML, "text/html; charset=utf-8")
            return

        if path == "/api/runs":
            snap = self.state.refresh_snapshot(force=True)
            runs = []
            for issue in snap["issues"]["issues"]:
                rid = issue.get("run_id")
                if rid:
                    runs.append({
                        "run_id": rid,
                        "issue_id": issue["issue_id"],
                        "status": issue["status"],
                        "workspace_path": issue.get("workspace_path", ""),
                    })
            self._send_json({"runs": runs})
            return

        if path.startswith("/api/runs/") and path.endswith("/events"):
            # /api/runs/{run_id}/events
            run_id = path[len("/api/runs/") : -len("/events")]
            self._stream_chat_events(run_id)
            return

        if path.startswith("/api/runs/") and "/tool-results/" in path:
            # /api/runs/{run_id}/tool-results/{call_id}
            parts = path[len("/api/runs/") :].split("/tool-results/")
            if len(parts) == 2:
                run_id = parts[0]
                self._send_json({"tool_result": "not yet available"}, status=501)
                return

        self.send_error(404, "Not Found")

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path or "/"

        

        self.send_error(404, "Not Found")

    # ----- SSE streaming ---------------------------------------------------

    def _stream_events(self) -> None:
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache, no-transform")
            self.send_header("Connection", "keep-alive")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
        except Exception:
            return

        snapshot_interval = self.state.snapshot_interval
        last_snapshot_at = 0.0

        try:
            # Unified storage: live event stream is sourced from
            # the session transcript JSONL.  We only push periodic
            # snapshot updates here — per-event streaming is handled
            # out-of-band by callers (e.g. ``issue takeover``) since the
            # transcript is a regular append-only file readable by
            # ``tail -f`` / TailFollower.
            snap = self.state.refresh_snapshot(force=True)
            last_snapshot_at = time.time()
            self._write_sse({"type": "snapshot", **snap})

            while True:
                # Throttle snapshot refreshes to once per snapshot_interval.
                now = time.time()
                if now - last_snapshot_at >= snapshot_interval:
                    snap = self.state.refresh_snapshot(force=True)
                    last_snapshot_at = now
                    self._write_sse({"type": "snapshot", **snap})

                # Heartbeat / keep-alive.
                self.wfile.write(b": ping\n\n")
                self.wfile.flush()

                time.sleep(snapshot_interval)
        except Exception:
            # Client disconnected or stream error — exit cleanly.
            return

    def _write_sse(self, payload: dict[str, Any]) -> None:
        data = json.dumps(payload, ensure_ascii=False, default=str)
        self.wfile.write(f"data: {data}\n\n".encode("utf-8"))
        self.wfile.flush()

    # ----- Chat SSE streaming ------------------------------------------------

    def _stream_chat_events(self, run_id: str) -> None:
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache, no-transform")
            self.send_header("Connection", "keep-alive")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
        except Exception:
            return

        gw: ChatGateway = self.state.chat_gateway

        # 1. Send history frame.
        history = gw.read_history(run_id)
        self._write_sse({"type": "history", "messages": history})

        # 2. Subscribe to live frames (after reading history to avoid races).
        live = gw.subscribe(run_id)
        if live is None:
            self._write_sse({"type": "RunEnded", "data": {"run_id": run_id}})
            return

        # 3. Send boundary frame.
        self._write_sse({"type": "boundary"})

        # 4. Stream live frames.
        try:
            while True:
                try:
                    frame = live.get(timeout=self.state.snapshot_interval)
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    continue
                self._write_sse({"type": "frame", "frame": frame})
                if frame.get("type") == "RunEnded":
                    break
        except Exception:
            pass
        finally:
            gw.unsubscribe(run_id, live)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def run(args: argparse.Namespace) -> int:
    """Execute the orchestrator dashboard command."""
    port: int = args.port
    host: str = args.host
    no_browser: bool = getattr(args, "no_browser", False)

    workspace = _resolve_workspace_root(getattr(args, "workspace", None))
    state = DashboardState(workspace)

    # Bind the per-process state to the handler class.
    DashboardHandler.state = state

    # Convert SIGTERM / SIGHUP into KeyboardInterrupt so the existing
    # except-block + atexit hooks clean up tailer threads.  Without this,
    # `kill <pid>` or closing the terminal would leave background threads
    # in an undefined state.
    def _graceful_signal(signum: int, frame: Any) -> None:
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, _graceful_signal)
    if hasattr(signal, "SIGHUP"):
        signal.signal(signal.SIGHUP, _graceful_signal)

    print(f"[dashboard] Workspace : {workspace}")
    print(f"[dashboard] Starting LiveView dashboard on http://{host}:{port}")
    if not workspace.exists():
        print(
            f"[dashboard] Note: workspace does not exist yet — UI will render empty until it is created."
        )

    try:
        server = ThreadingHTTPServer((host, port), DashboardHandler)
        threading.Thread(target=server.serve_forever, name="DashboardHTTP", daemon=True).start()

        if not no_browser:
            try:
                import webbrowser

                webbrowser.open(f"http://{host}:{port}")
            except Exception:
                pass

        print(f"[dashboard] Serving at http://{host}:{port}", file=sys.stderr)
        print("[dashboard] Press Ctrl+C to stop", file=sys.stderr)

        # Park the main thread.
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n[dashboard] stopped")
    except OSError as exc:
        print(f"[dashboard] error: {exc}", file=sys.stderr)
        return 1
    finally:
        # Explicit cleanup (belt-and-suspenders alongside atexit hooks).
        state.tailer_manager.stop_all()
        state.chat_gateway.stop()

    return 0
