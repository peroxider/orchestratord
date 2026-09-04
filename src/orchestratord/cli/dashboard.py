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
import uuid
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from ..chat_gateway import ChatGateway
from ..conversation_store import ConversationStore
from ..event_tailer import EventTailerManager
from ..paths import ORCHESTRATOR_DIR, ORCHESTRATORD_BASE
from ..run_read_model import RunReadModel
from ..tracker import Intent
from .dashboard_ui import LIVEVIEW_HTML

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Issue status taxonomy
# ---------------------------------------------------------------------------
# These constants MUST stay in sync with the orchestrator core's IssueStatus
# enum. We re-declare them here so the dashboard server can be loaded
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
                except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
                    logger.debug("Ignoring unreadable dashboard metadata %s: %s", mf, exc)
                    continue
        if candidates:
            candidates.sort(key=lambda c: c[0], reverse=True)
            return candidates[0][1]

    return ORCHESTRATORD_BASE / "workspace"


# ---------------------------------------------------------------------------
# State aggregation
# ---------------------------------------------------------------------------


def _safe_read_json(path: Path) -> dict[str, Any] | None:
    """Best-effort JSON object reader for the follow-up mutation path."""
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, TypeError, ValueError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _gather_issue_metadata(workspace: Path) -> dict[str, Any]:
    """Compatibility wrapper around the server-side run read model."""
    return RunReadModel(workspace).read()


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
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
            logger.debug("Ignoring unreadable dashboard metadata %s: %s", mf, exc)
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


def _related_session_run_ids(run_id: str) -> list[str]:
    """Return current and older session IDs that belong to one issue."""
    from ..paths import SESSIONS_DIR

    parts = run_id.split("_", 2)
    if len(parts) < 3 or not SESSIONS_DIR.exists():
        return []
    issue_suffix = "_" + parts[2]
    try:
        return sorted(
            entry.name
            for entry in SESSIONS_DIR.iterdir()
            if entry.is_dir()
            and entry.name.endswith(issue_suffix)
            and entry.name <= run_id
        )
    except OSError as exc:
        logger.debug("Unable to discover related sessions for %s: %s", run_id, exc)
        return []


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------


class DashboardState:
    """Per-process shared state for the dashboard HTTP server."""

    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace
        self.read_model = RunReadModel(workspace)
        self.revision = 0
        self.event_cursor = 0
        # Cursors are process-local.  The browser uses this epoch to discard
        # cached observations after a dashboard restart instead of merging
        # two unrelated cursor sequences.
        self.event_epoch = uuid.uuid4().hex
        self._stable_snapshot_signature = ""
        self.snapshot: dict[str, Any] = {
            "type": "snapshot",
            "ts": time.time(),
            "revision": self.revision,
            "event_epoch": self.event_epoch,
            "event_cursor": self.event_cursor,
            "workspace": str(workspace),
            "workspace_exists": workspace.exists(),
            "metadata": _gather_metadata(workspace),
            "issues": self.read_model.read(),
            "events": {
                "total": 0,
                "by_type": {},
                "by_run": {},
                "recent": [],
                "recent_limit": 200,
            },
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
        self._event_log: deque[dict[str, Any]] = deque(maxlen=1000)
        self._observations_by_run: dict[str, deque[dict[str, Any]]] = {}
        self._event_by_type: dict[str, int] = {}
        self._event_by_run: dict[str, int] = {}
        # Track completed issues whose historical events have been loaded
        self._loaded_historical: set[str] = set()
        # Track run_ids that were ever actively tailed, to prevent
        # load_historical() from re-reading files the tailer already consumed
        self._ever_tailed: set[str] = set()
        # A current run ID changes whenever a follow-up starts, so it is a
        # natural cache key for the immutable set of earlier session IDs.
        self._related_session_cache: dict[str, tuple[str, ...]] = {}

    def refresh_snapshot(self, force: bool = False) -> dict[str, Any]:
        now = time.time()
        with self._lock:
            if force or (now - self.last_snapshot_at) >= self.snapshot_interval:
                # 1. Read registry + metadata
                observations = {
                    run_id: list(events)
                    for run_id, events in self._observations_by_run.items()
                }
                issues = self.read_model.read(observations)
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

                # 2c. Sync chat gateway connections only for live agent
                # sessions. Workflow-active states such as pending_review do
                # not own a writable control channel.
                run_id_to_endpoint: dict[str, str] = {}
                for issue in issues.get("issues", []):
                    rid = issue.get("run_id")
                    ws = issue.get("workspace_path")
                    if not rid or not ws or not _issue_run_is_live(issue):
                        continue
                    ws_path = Path(ws)
                    sock_path = Path(ws_path) / ".run_control" / f"{rid}.sock"
                    endpoint_file = sock_path.with_suffix(".endpoint.json")
                    if endpoint_file.exists():
                        try:
                            endpoint = json.loads(endpoint_file.read_text(encoding="utf-8"))["endpoint"]
                            if isinstance(endpoint, str) and endpoint.startswith("tcp://127.0.0.1:"):
                                run_id_to_endpoint[rid] = endpoint
                                continue
                        except (OSError, ValueError, KeyError, TypeError):
                            pass
                    if sock_path.exists():
                        run_id_to_endpoint[rid] = str(sock_path)
                self.chat_gateway.sync_active_run_ids(run_id_to_endpoint)

                # 2b. One-shot replay for completed runs and earlier sessions
                # in the same issue conversation. This keeps historical
                # Evidence available after a dashboard restart without
                # mixing it into the latest run's state or metrics.
                for issue in issues.get("issues", []):
                    rid = issue.get("run_id")
                    ws = issue.get("workspace_path")
                    if not rid or not ws:
                        continue
                    related = self._related_session_cache.setdefault(
                        rid, tuple(_related_session_run_ids(rid))
                    )
                    replay_ids = set(related)
                    if issue.get("status") not in ACTIVE_STATUSES:
                        replay_ids.add(rid)
                    for replay_id in sorted(replay_ids):
                        if (
                            replay_id in self._loaded_historical
                            or replay_id in self._ever_tailed
                        ):
                            continue
                        self.tailer_manager.load_historical(
                            replay_id, issue["issue_id"], Path(ws)
                        )
                        self._loaded_historical.add(replay_id)

                # 3. Drain events, update rolling buffers
                for evt in self.tailer_manager.drain_events():
                    self.event_cursor += 1
                    evt = dict(evt)
                    evt["cursor"] = self.event_cursor
                    self._recent_events.appendleft(evt)
                    self._event_log.append(evt)
                    et = evt.get("event_type", "unknown")
                    self._event_by_type[et] = self._event_by_type.get(et, 0) + 1
                    run_id = str(evt.get("run_id") or "")
                    if run_id:
                        self._event_by_run[run_id] = self._event_by_run.get(run_id, 0) + 1
                        run_events = self._observations_by_run.setdefault(
                            run_id, deque(maxlen=2000)
                        )
                        run_events.append(evt)

                # Newly drained observations may add token telemetry or change
                # the timestamp-quality label, so join the sources once more.
                observations = {
                    run_id: list(events)
                    for run_id, events in self._observations_by_run.items()
                }
                issues = self.read_model.read(observations)
                for issue in issues.get("issues", []):
                    run_id = str(issue.get("run_id") or "")
                    issue["chat_control_available"] = bool(
                        run_id
                        and _issue_run_is_live(issue)
                        and self.chat_gateway.is_control_ready(run_id)
                    )

                # 4. Assemble snapshot with events + token_activity
                candidate = {
                    "type": "snapshot",
                    "ts": time.time(),
                    "revision": self.revision,
                    "event_epoch": self.event_epoch,
                    "event_cursor": self.event_cursor,
                    "workspace": str(self.workspace),
                    "workspace_exists": self.workspace.exists(),
                    "metadata": meta,
                    "issues": issues,
                    "events": {
                        "total": sum(self._event_by_type.values()),
                        "by_type": dict(self._event_by_type),
                        "by_run": dict(self._event_by_run),
                        "recent": list(self._recent_events)[:200],
                        "recent_limit": self._recent_events.maxlen,
                    },
                    "token_activity": self._build_token_activity(issues),
                }
                signature = self._snapshot_revision_signature(candidate)
                if signature != self._stable_snapshot_signature:
                    self.revision += 1
                    self._stable_snapshot_signature = signature
                candidate["revision"] = self.revision
                self.snapshot = candidate
                self.last_snapshot_at = now
            return self.snapshot

    @staticmethod
    def _snapshot_revision_signature(snapshot: dict[str, Any]) -> str:
        """Hash only state changes that require a new full snapshot.

        Wall-clock ages and observation counters are intentionally excluded:
        the browser derives age locally and receives observations as deltas.
        """
        metadata = snapshot.get("metadata") or {}
        stable_metadata = {
            key: metadata.get(key)
            for key in (
                "found",
                "pid",
                "alive",
                "started_at",
                "project_slug",
                "workflow_path",
                "metadata_path",
            )
        }
        stable_issues = []
        for issue in (snapshot.get("issues") or {}).get("issues", []):
            stable_issues.append(
                {
                    key: value
                    for key, value in issue.items()
                    if key not in {"age_seconds", "idle_seconds", "data_quality"}
                }
            )
        return json.dumps(
            {
                "workspace_exists": snapshot.get("workspace_exists"),
                "metadata": stable_metadata,
                "issues": stable_issues,
            },
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        )

    def events_after(
        self, cursor: int
    ) -> tuple[list[dict[str, Any]], bool]:
        """Return global SSE deltas after *cursor* and whether history rolled off."""
        with self._lock:
            if not self._event_log:
                return [], False
            first = int(self._event_log[0].get("cursor") or 0)
            truncated = cursor > 0 and cursor < first - 1
            return (
                [
                    event
                    for event in self._event_log
                    if int(event.get("cursor") or 0) > cursor
                ],
                truncated,
            )

    def observations(
        self, run_id: str, *, cursor: int = 0, limit: int = 500
    ) -> dict[str, Any]:
        """Return a bounded, cursor-based page of one run's observations."""
        safe_limit = max(1, min(limit, 1000))
        with self._lock:
            stored = list(self._observations_by_run.get(run_id, ()))
            page = [
                event
                for event in stored
                if int(event.get("cursor") or 0) > max(0, cursor)
            ][:safe_limit]
            next_cursor = (
                int(page[-1].get("cursor") or cursor) if page else max(0, cursor)
            )
            total = self._event_by_run.get(run_id, len(stored))
            return {
                "run_id": run_id,
                "observations": page,
                "visible_total": len(stored),
                "captured_total": total,
                "cursor": max(0, cursor),
                "next_cursor": next_cursor,
                "has_more": len(
                    [
                        event
                        for event in stored
                        if int(event.get("cursor") or 0) > next_cursor
                    ]
                )
                > 0,
                "buffer_limit": 2000,
            }

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
LIVEVIEW_DOCUMENT = LIVEVIEW_HTML


def _build_dashboard_html() -> str:
    """Inject the STATUS_META JSON into the HTML template."""
    return LIVEVIEW_DOCUMENT.replace(
        "__STATUS_META__",
        json.dumps(STATUS_META, ensure_ascii=False),
    )


def _conversation_transcript_rows(conversation_id: str) -> list[dict[str, Any]]:
    """Read all v2/legacy transcript rows referenced by a manifest."""
    manifest = ConversationStore().get(conversation_id)
    if not manifest:
        return []
    rows: list[dict[str, Any]] = []
    for run in manifest.get("runs", []):
        run_id = str(run.get("run_id") or "")
        if not run_id or "/" in run_id or "\\" in run_id:
            continue
        path = Path.home() / ".orchestratord" / "sessions" / run_id / "transcript.jsonl"
        if not path.exists():
            path = Path.home() / ".cache" / "orchestratord" / "sessions" / run_id / "transcript.jsonl"
        try:
            with path.open(encoding="utf-8") as stream:
                for line in stream:
                    try:
                        row = json.loads(line)
                    except (TypeError, ValueError, json.JSONDecodeError):
                        continue
                    if not isinstance(row, dict):
                        continue
                    # Old rows have no identity metadata; the manifest is
                    # authoritative for their owning run.
                    row.setdefault("run_id", run_id)
                    row.setdefault("conversation_id", conversation_id)
                    for key in ("backend", "backend_session_id", "stage_id", "branch_id", "parent_run_id"):
                        if key not in row and run.get(key) is not None:
                            row[key] = run[key]
                    rows.append(row)
        except (FileNotFoundError, OSError):
            continue
    def _sort_key(row: dict[str, Any]) -> tuple[float, int]:
        value = row.get("timestamp", row.get("ts", 0))
        try:
            stamp = float(value)
        except (TypeError, ValueError):
            stamp = 0.0
        try:
            seq = int(row.get("seq") or 0)
        except (TypeError, ValueError):
            seq = 0
        return stamp, seq

    rows.sort(key=_sort_key)
    return rows


# ---------------------------------------------------------------------------
# HTTP server
# ---------------------------------------------------------------------------


def _previous_run_histories(run_id: str) -> list[dict[str, Any]]:
    """Return captured histories for older runs of the same issue.

    The returned records retain the owning ``run_id`` so browser clients can
    present issue-level conversation continuity without pretending that every
    message belongs to the latest run.
    """
    try:
        from ..event_tailer import read_history_direct
        histories: list[dict[str, Any]] = []
        for previous_run_id in _related_session_run_ids(run_id):
            if previous_run_id == run_id:
                continue
            messages = read_history_direct(previous_run_id)
            if messages:
                histories.append(
                    {
                        "run_id": previous_run_id,
                        "current": False,
                        "messages": messages,
                    }
                )
        return histories
    except Exception as exc:  # noqa: BLE001 - history discovery is best effort
        logger.debug("Unable to discover previous history for %s: %s", run_id, exc)
        return []


def _conversation_history_sessions(
    run_id: str,
    current_history: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Return the issue conversation as explicit per-run sessions."""
    previous = _previous_run_histories(run_id)
    followup_messages: list[dict[str, Any]] = []
    if previous:
        # Completed-run follow-ups are written before the replacement run has
        # an id, so their durable write initially lands at the tail of the
        # previous transcript.  Present trailing unanswered operator messages
        # with the replacement run that they started.  ``origin=followup`` is
        # authoritative for new records; the trailing-user fallback preserves
        # correct ownership for older transcripts written before that marker.
        messages = previous[-1].get("messages") or []
        while messages:
            candidate = messages[-1]
            role = str(candidate.get("role") or "").lower()
            if role not in {"user", "human"}:
                break
            followup_messages.insert(0, messages.pop())

    return [
        *previous,
        {
            "run_id": run_id,
            "current": True,
            "messages": followup_messages + current_history
            if followup_messages
            else current_history,
        },
    ]


def _issue_run_is_live(issue: dict[str, Any]) -> bool:
    """Return whether an issue still owns a writable agent control session."""
    return issue.get("status") == "running" or bool(issue.get("pause_reason"))


def _snapshot_run_is_active(snapshot: dict[str, Any], run_id: str) -> bool:
    """Return whether the latest registry snapshot still owns a live run."""
    issues = (snapshot.get("issues") or {}).get("issues") or []
    return any(
        str(issue.get("run_id") or "") == run_id
        and _issue_run_is_live(issue)
        for issue in issues
    )


def _flatten_previous_run_history(
    sessions: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Flatten previous sessions for legacy chat clients."""
    merged: list[dict[str, Any]] = []
    for index, session in enumerate(sessions):
        merged.extend(session.get("messages") or [])
        if index < len(sessions) - 1:
            previous_run_id = str(session.get("run_id") or "")
            merged.append(
                {
                    "role": "system",
                    "content": f"--- Previous session ({previous_run_id[9:15]}) ---",
                    "ts": "",
                }
            )
    if merged:
        merged.append(
            {
                "role": "system",
                "content": "--- New follow-up session ---",
                "ts": "",
            }
        )
    return merged


def _merge_previous_run_history(run_id: str) -> list[dict[str, Any]]:
    """Merge transcript history from all previous runs of the same issue.

    When a followup creates a new run, the new transcript starts empty.
    This function collects the full history from every previous run for
    the same issue so the chat UI preserves the complete conversation
    context across followup chains.

    Returns an empty list if no previous runs are found.
    """
    return _flatten_previous_run_history(_previous_run_histories(run_id))


def _followup_completed_run(workspace: Path, run_id: str, text: str) -> bool:
    """Queue a follow-up for a completed session.

    Writes the follow-up text to ``.operator_hints.md`` in the issue's
    workspace, marks ``Intent.FOLLOWUP`` in the registry, and drops a
    ``chat_followup`` control file so the daemon re-launches the issue
    without resetting the existing PR / branch.

    Returns ``True`` if the follow-up was queued successfully.
    """
    registry_path = workspace / ".orchestratord_issue_registry.json"
    raw = _safe_read_json(registry_path) or {}

    # Find the issue record by run_id.
    issue_id = ""
    issue_workspace_path = ""
    for rid, record in raw.items():
        if not isinstance(record, dict):
            continue
        if record.get("run_id") == run_id:
            issue_id = rid
            issue_workspace_path = record.get("workspace_path") or ""
            break

    if not issue_id:
        logger.warning("_followup_completed_run: no issue found for run_id=%s", run_id)
        return False

    # Write the follow-up text to .operator_hints.md so prompt_builder
    # prepends it to the agent's context on re-launch.
    if issue_workspace_path:
        try:
            hints_file = Path(issue_workspace_path) / ".operator_hints.md"
            timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
            header = f"--- Chat Follow-up (sent at {timestamp}) ---\n"
            separator = "\n" + "-" * 50 + "\n"
            with open(hints_file, "a", encoding="utf-8") as f:
                f.write(header)
                f.write(text + "\n")
                f.write(separator)
        except Exception:
            logger.exception(
                "Failed to write operator_hints for run_id=%s", run_id
            )
            return False

    # Mark intent in the registry.
    try:
        from ..issue_registry import IssueRegistry

        registry = IssueRegistry(registry_path)
        registry.mark_intent(
            issue_id,
            Intent.FOLLOWUP,
            source="chat",
            command=f"chat:followup:{text[:64]}",
        )
    except Exception:
        logger.exception(
            "Failed to mark intent for issue_id=%s", issue_id
        )
        return False

    # Write a control file so the daemon picks this up immediately
    # instead of waiting for the next poll cycle.
    try:
        control_dir = workspace / ".orchestrator_control"
        control_dir.mkdir(parents=True, exist_ok=True)
        control_file = control_dir / f"followup_{issue_id}.control"
        control_file.write_text(
            f"followup\n{issue_id}\n{text}\n", encoding="utf-8"
        )
    except Exception:
        logger.exception(
            "Failed to write control file for issue_id=%s", issue_id
        )
        return False

    logger.info(
        "Chat follow-up queued for issue_id=%s run_id=%s",
        issue_id,
        run_id,
    )

    # Persist the follow-up message in the old session's transcript so it
    # survives page refreshes.  The transcript is a JSONL file at
    # ~/.orchestratord/sessions/<run_id>/transcript.jsonl.
    try:
        from pathlib import Path as _Path

        from ..paths import SESSIONS_DIR

        transcript_path = _Path(SESSIONS_DIR) / run_id / "transcript.jsonl"
        if transcript_path.exists():
            entry = json.dumps(
                {
                    "role": "user",
                    "content": text,
                    "origin": "followup",
                    "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                }
            )
            with open(transcript_path, "a", encoding="utf-8") as tf:
                tf.write(entry + "\n")
    except Exception:
        logger.exception(
            "Failed to append followup to transcript run_id=%s", run_id
        )

    return True


class DashboardHandler(BaseHTTPRequestHandler):
    """HTTP handler serving the dashboard UI, JSON snapshots, and SSE events."""

    server_version = "OrchestratordDashboard/1.0"
    state: DashboardState  # set on the class by run()

    # Quieter logs — one line per request is too noisy for a polling UI.
    def log_message(self, format: str, *args: Any) -> None:
        return

    def _send_json(self, payload: Any, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _send_text(self, text: str, content_type: str, status: int = 200) -> None:
        body = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
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
            self._send_text(_build_dashboard_html(), "text/html; charset=utf-8")
            return

        if path == "/api/runs":
            snap = self.state.refresh_snapshot(force=True)
            runs = []
            for issue in snap["issues"]["issues"]:
                rid = issue.get("run_id")
                if rid:
                    runs.append({
                        "run_id": rid,
                        "conversation_id": issue.get("conversation_id") or rid,
                        "backend": (issue.get("execution") or {}).get("backend", ""),
                        "stage_id": issue.get("stage_id"),
                        "branch_id": issue.get("branch_id"),
                        "issue_id": issue["issue_id"],
                        "status": issue["status"],
                        "workspace_path": issue.get("workspace_path", ""),
                    })
            self._send_json({"runs": runs, "server_ts": time.time()})
            return

        if path == "/api/conversations":
            manifests = ConversationStore().list()
            self._send_json({
                "conversations": [
                    {
                        **manifest,
                        "run_count": len(manifest.get("runs", [])),
                    }
                    for manifest in manifests
                ],
                "server_ts": time.time(),
            })
            return

        if path.startswith("/api/conversations/"):
            suffix = path[len("/api/conversations/") :]
            if "/" in suffix:
                conversation_id, operation = suffix.split("/", 1)
            else:
                conversation_id, operation = suffix, ""
            if not conversation_id or "/" in conversation_id or "\\" in conversation_id:
                self._send_json({"error": "invalid conversation id"}, status=400)
                return
            manifest = ConversationStore().get(conversation_id)
            if manifest is None:
                manifest = ConversationStore().ensure_legacy_run(conversation_id)
            if manifest is None:
                self._send_json({"error": "conversation not found", "conversation_id": conversation_id}, status=404)
                return
            if operation == "events":
                self._stream_conversation_events(conversation_id)
                return
            if operation:
                self.send_error(404, "Not Found")
                return
            rows = _conversation_transcript_rows(conversation_id)
            self._send_json({"conversation": manifest, "events": rows, "conversation_id": conversation_id})
            return

        if path.startswith("/api/runs/") and path.endswith("/events"):
            # /api/runs/{run_id}/events
            run_id = path[len("/api/runs/") : -len("/events")]
            self._stream_chat_events(run_id)
            return

        if path.startswith("/api/runs/") and path.endswith("/observations"):
            # Cursor pagination keeps a long run independent from the global
            # dashboard buffer used by the Overview snapshot.
            run_id = path[len("/api/runs/") : -len("/observations")]
            if not run_id or "/" in run_id or "\\" in run_id:
                self._send_json({"error": "invalid run id"}, status=400)
                return
            query = parse_qs(parsed.query)
            try:
                cursor = max(0, int((query.get("cursor") or ["0"])[0]))
                limit = int((query.get("limit") or ["500"])[0])
            except (TypeError, ValueError):
                self._send_json({"error": "cursor and limit must be integers"}, status=400)
                return
            self.state.refresh_snapshot(force=True)
            self._send_json(
                self.state.observations(run_id, cursor=cursor, limit=limit)
            )
            return

        if path.startswith("/api/runs/") and "/tool-results/" in path:
            # /api/runs/{run_id}/tool-results/{call_id} — lazy full-text
            # completion for truncated ToolResult SSE frames.
            parts = path[len("/api/runs/") :].split("/tool-results/")
            if len(parts) == 2:
                run_id, call_id = parts[0], parts[1]
                try:
                    from ..event_tailer import read_tool_result

                    result = read_tool_result(run_id, call_id)
                except Exception:
                    logger.exception("tool-results lookup failed run_id=%s", run_id)
                    result = None
                if result is None:
                    self._send_json(
                        {"error": f"tool result {call_id!r} not found for run {run_id!r}"},
                        status=404,
                    )
                    return
                self._send_json(result)
                return

        self.send_error(404, "Not Found")

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path or "/"
        conversation_prefix = "/api/conversations/"
        if path.startswith(conversation_prefix) and path.endswith("/messages"):
            conversation_id = path[len(conversation_prefix) : -len("/messages")]
            if not conversation_id or "/" in conversation_id or "\\" in conversation_id:
                self._send_json({"error": "invalid conversation id"}, status=400)
                return
            body: dict[str, Any] = {}
            try:
                content_length = int(self.headers.get("Content-Length", "0"))
                if content_length > 64 * 1024:
                    self._send_json({"error": "request body too large"}, status=413)
                    return
                if content_length:
                    body = json.loads(self.rfile.read(content_length).decode("utf-8"))
                    if not isinstance(body, dict):
                        raise ValueError("request body must be an object")
            except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
                self._send_json({"error": "invalid JSON body"}, status=400)
                return
            text = body.get("text")
            if not isinstance(text, str) or not text.strip():
                self._send_json({"error": "text is required"}, status=400)
                return
            manifest = ConversationStore().get(conversation_id)
            if manifest is None:
                self._send_json({"error": "conversation not found"}, status=404)
                return
            runs = manifest.get("runs", [])
            run_id = next((str(run.get("run_id")) for run in reversed(runs) if run.get("status") == "running"), None)
            if run_id and self.state.chat_gateway.send_message(run_id, text.strip()):
                self._send_json({"accepted": True, "conversation_id": conversation_id, "run_id": run_id}, status=202)
                return
            if runs:
                run_id = str(runs[-1].get("run_id") or "")
                if run_id and _followup_completed_run(self.state.workspace, run_id, text.strip()):
                    self._send_json({"accepted": True, "conversation_id": conversation_id, "run_id": run_id, "mode": "followup_queued"}, status=202)
                    return
            self._send_json({"error": "conversation has no writable run", "conversation_id": conversation_id}, status=409)
            return

        prefix = "/api/runs/"
        if not path.startswith(prefix):
            self.send_error(404, "Not Found")
            return

        suffixes = ("/messages", "/pause", "/resume", "/stop")
        suffix = next((item for item in suffixes if path.endswith(item)), None)
        if suffix is None:
            self.send_error(404, "Not Found")
            return
        run_id = path[len(prefix) : -len(suffix)]
        if not run_id or "/" in run_id or "\\" in run_id:
            self._send_json({"error": "invalid run id"}, status=400)
            return

        body: dict[str, Any] = {}
        try:
            content_length = int(self.headers.get("Content-Length", "0"))
            if content_length > 64 * 1024:
                self._send_json({"error": "request body too large"}, status=413)
                return
            if content_length:
                raw = self.rfile.read(content_length)
                body = json.loads(raw.decode("utf-8"))
                if not isinstance(body, dict):
                    raise ValueError("request body must be an object")
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
            self._send_json({"error": "invalid JSON body"}, status=400)
            return

        gateway: ChatGateway = self.state.chat_gateway
        if suffix == "/messages":
            text = body.get("text")
            if not isinstance(text, str) or not text.strip():
                self._send_json({"error": "text is required"}, status=400)
                return
            ok = gateway.send_message(run_id, text.strip())
            if not ok:
                snapshot = self.state.refresh_snapshot(force=True)
                if _snapshot_run_is_active(snapshot, run_id):
                    # A new run can become visible just before its control
                    # socket is ready. Do not reinterpret that delivery race
                    # as a request to start another provider run.
                    self._send_json(
                        {
                            "error": "run control channel is not ready",
                            "run_id": run_id,
                            "retryable": True,
                        },
                        status=409,
                    )
                    return
                # The session is durably complete. Fall back to the registry
                # follow-up path and let the daemon create one replacement
                # run on the same issue and branch.
                queued = _followup_completed_run(
                    self.state.workspace, run_id, text.strip()
                )
                if not queued:
                    self._send_json(
                        {"error": "run not active and followup queue failed",
                         "run_id": run_id},
                        status=409,
                    )
                    return
                self._send_json(
                    {"accepted": True, "run_id": run_id, "mode": "followup_queued"},
                    status=202,
                )
                return
        else:
            verb = suffix[1:]
            payload = body.get("message", "") if verb == "resume" else ""
            if not isinstance(payload, str):
                self._send_json({"error": "message must be a string"}, status=400)
                return
            ok = gateway.control(run_id, verb, payload)

        if not ok:
            self._send_json({"error": "run not active", "run_id": run_id}, status=409)
            return
        self._send_json({"accepted": True, "run_id": run_id}, status=202)

    # ----- SSE streaming ---------------------------------------------------

    def _stream_events(self) -> None:
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache, no-transform")
            self.send_header("Connection", "keep-alive")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
        except (BrokenPipeError, ConnectionResetError, OSError):
            return

        snapshot_interval = self.state.snapshot_interval

        try:
            snap = self.state.refresh_snapshot(force=True)
            self._write_sse({"type": "snapshot", **snap})
            last_revision = int(snap.get("revision") or 0)
            last_event_cursor = int(snap.get("event_cursor") or 0)

            while True:
                # All browser tabs share DashboardState's bounded refresh
                # interval.  A forced filesystem scan per SSE client made
                # multi-tab observation progressively slower.
                snap = self.state.refresh_snapshot(force=False)
                revision = int(snap.get("revision") or 0)
                if revision != last_revision:
                    # Registry/report/daemon truth changed. A fresh snapshot
                    # is simpler and safer than a field-level patch; advance
                    # the event cursor because its bounded recent window is
                    # already included in this frame.
                    self._write_sse({"type": "snapshot", **snap})
                    last_revision = revision
                    last_event_cursor = int(snap.get("event_cursor") or 0)
                else:
                    deltas, truncated = self.state.events_after(last_event_cursor)
                    if truncated:
                        # This client lagged beyond the retained delta log.
                        # Rebase it with one full snapshot instead of silently
                        # dropping observations.
                        self._write_sse({"type": "snapshot", **snap})
                        last_event_cursor = int(snap.get("event_cursor") or 0)
                    else:
                        for event in deltas:
                            self._write_sse(event)
                            last_event_cursor = int(
                                event.get("cursor") or last_event_cursor
                            )

                # Heartbeat / keep-alive.
                self.wfile.write(b": ping\n\n")
                self.wfile.flush()

                time.sleep(snapshot_interval)
        except (BrokenPipeError, ConnectionResetError, OSError):
            # Client disconnected or stream error — exit cleanly.
            return

    def _write_sse(self, payload: dict[str, Any]) -> None:
        data = json.dumps(payload, ensure_ascii=False, default=str)
        self.wfile.write(f"data: {data}\n\n".encode())
        self.wfile.flush()

    # ----- Chat SSE streaming ------------------------------------------------

    def _chat_history_payload(
        self,
        gateway: ChatGateway,
        run_id: str,
    ) -> dict[str, Any]:
        """Build one replay frame with explicit per-run conversation history."""
        history = gateway.read_history(run_id)
        sessions = _conversation_history_sessions(run_id, history)
        for session in sessions:
            coverage = self.state.observations(
                str(session.get("run_id") or ""),
                limit=1,
            )
            session["evidence_count"] = coverage.get("captured_total", 0)
        previous = _flatten_previous_run_history(sessions[:-1])
        if previous:
            history = previous + history
        return {
            "type": "history",
            "messages": history,
            "sessions": sessions,
            "current_run_id": run_id,
        }

    def _stream_chat_events(self, run_id: str) -> None:
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache, no-transform")
            self.send_header("Connection", "keep-alive")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
        except (BrokenPipeError, ConnectionResetError, OSError):
            return

        gw: ChatGateway = self.state.chat_gateway

        # Subscribe first so frames produced while history is being read are
        # buffered rather than silently lost at the history/live boundary.
        live = gw.subscribe(run_id)

        # Chat can be the first dashboard request after startup. Refresh now
        # so earlier run transcripts are replayed before evidence counts are
        # attached to the history frame. A follow-up can also expose its run
        # ID one refresh before the control socket exists. In that case the
        # first subscribe above returns None; refresh and retry briefly instead
        # of misreporting the still-starting run as ended.
        snapshot = self.state.refresh_snapshot(force=True)
        if live is None:
            deadline = time.monotonic() + 1.0
            while time.monotonic() < deadline:
                if not _snapshot_run_is_active(snapshot, run_id):
                    break
                live = gw.subscribe(run_id)
                if live is not None:
                    break
                time.sleep(0.05)
                snapshot = self.state.refresh_snapshot(force=True)

        # Always replay history — even for completed sessions where
        # subscribe() returns None because the control socket is gone.
        self._write_sse(self._chat_history_payload(gw, run_id))
        self._write_sse({"type": "boundary"})

        if live is None:
            event_type = (
                "RunUnavailable"
                if _snapshot_run_is_active(snapshot, run_id)
                else "RunEnded"
            )
            self._write_sse({"type": event_type, "data": {"run_id": run_id}})
            # Give the browser a moment to process the history + lifecycle
            # event before closing the connection. Without this delay,
            # EventSource may fire onerror before onmessage and drop history.
            time.sleep(0.5)
            return

        # Stream live frames.
        try:
            while True:
                try:
                    frame = live.get(timeout=self.state.snapshot_interval)
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    continue
                if frame.get("type") == "RunEnded":
                    # Some backends persist only a final transcript and emit
                    # no TextDelta frames. Re-read it before the browser sees
                    # RunEnded, because that event closes the EventSource.
                    self._write_sse(self._chat_history_payload(gw, run_id))
                    self._write_sse({"type": "frame", "frame": frame})
                    break
                self._write_sse({"type": "frame", "frame": frame})
        except (BrokenPipeError, ConnectionResetError, OSError) as exc:
            logger.debug("Chat SSE disconnected for run_id=%s: %s", run_id, exc)
        finally:
            gw.unsubscribe(run_id, live)

    def _stream_conversation_events(self, conversation_id: str) -> None:
        """SSE stream for a logical conversation, retaining run metadata."""
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache, no-transform")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            rows = _conversation_transcript_rows(conversation_id)
            self._write_sse({
                "type": "conversation",
                "conversation_id": conversation_id,
                "events": rows,
            })
            self._write_sse({"type": "boundary", "conversation_id": conversation_id})
            sent = len(rows)
            while True:
                time.sleep(self.state.snapshot_interval)
                rows = _conversation_transcript_rows(conversation_id)
                for row in rows[sent:]:
                    self._write_sse({
                        "type": "event",
                        "conversation_id": conversation_id,
                        "event": row,
                    })
                sent = len(rows)
                self.wfile.write(b": ping\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            return


class DashboardHTTPServer(ThreadingHTTPServer):
    """Threaded server that keeps routine browser disconnects out of logs."""

    daemon_threads = True

    def handle_error(self, request: Any, client_address: Any) -> None:
        error = sys.exc_info()[1]
        if isinstance(error, (BrokenPipeError, ConnectionResetError)):
            return
        super().handle_error(request, client_address)


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
            "[dashboard] Note: workspace does not exist yet — UI will render empty until it is created."
        )

    try:
        server = DashboardHTTPServer((host, port), DashboardHandler)
        threading.Thread(target=server.serve_forever, name="DashboardHTTP", daemon=True).start()

        if not no_browser:
            try:
                import webbrowser

                webbrowser.open(f"http://{host}:{port}")
            except Exception as exc:  # noqa: BLE001 - opening a browser is optional
                logger.debug("Unable to open dashboard browser: %s", exc)

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
