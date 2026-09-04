"""Server-side read model for dashboard run state.

The durable sources remain the issue registry, per-run reports, and session
observations.  This module owns the joins and the truth labels so browser
clients do not have to reverse-engineer execution state from loosely related
files.
"""

from __future__ import annotations

import json
import time
from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from .conversation_store import ConversationStore

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
ACTIVE_STATUSES = frozenset(
    {"queued", "pending", "running", "synced", "pending_review"}
)
TERMINAL_STATUSES = frozenset(
    {"completed", "failed", "abandoned", "verification_failed"}
)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, TypeError, ValueError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _parse_timestamp(value: Any) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return float(value)
    except ValueError:
        pass
    try:
        return datetime.fromisoformat(value).timestamp()
    except ValueError:
        return None


class RunReadModel:
    """Join durable run sources into a presentation-ready, truthful model.

    ``read`` is the module's primary interface.  Callers may provide the
    observations already held by their live tailer; the model then adds
    timing quality and execution telemetry without reading the transcript a
    second time.
    """

    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace

    def read(
        self,
        observations_by_run: Mapping[str, Sequence[dict[str, Any]]] | None = None,
        *,
        now: float | None = None,
    ) -> dict[str, Any]:
        registry = _read_json(
            self.workspace / ".orchestratord_issue_registry.json"
        )
        observed = observations_by_run or {}
        current_time = time.time() if now is None else now
        issues: list[dict[str, Any]] = []
        by_status = {status: 0 for status in ISSUE_STATUSES}

        for issue_id, record in registry.items():
            if not isinstance(record, dict):
                continue
            status = self._status(record.get("status"))
            by_status[status] += 1
            run_id = str(record.get("run_id") or "")
            report = self._read_report(record, run_id)
            observations = list(observed.get(run_id, ())) if run_id else []
            issue = self._issue_view(
                str(issue_id), record, report, observations, current_time
            )
            issues.append(issue)

        issues.sort(
            key=lambda issue: (
                0 if issue["status"] in ACTIVE_STATUSES else 1,
                -int(issue["updated_at"] or 0),
            )
        )
        return {
            "issues": issues,
            "by_status": by_status,
            "totals": {
                "total": len(issues),
                "active": sum(
                    issue["status"] in ACTIVE_STATUSES for issue in issues
                ),
                "terminal": sum(
                    issue["status"] in TERMINAL_STATUSES for issue in issues
                ),
                "prs": sum(bool(issue.get("pr_number")) for issue in issues),
                "attention": sum(
                    bool(issue["attention"]["required"]) for issue in issues
                ),
            },
        }

    @staticmethod
    def _status(value: Any) -> str:
        if not isinstance(value, str):
            return "pending"
        normalized = value.strip().lower()
        return normalized if normalized in ISSUE_STATUSES else "pending"

    @staticmethod
    def _read_report(record: dict[str, Any], run_id: str) -> dict[str, Any]:
        workspace_path = str(record.get("workspace_path") or "")
        if not run_id:
            return {}
        candidates: list[Path] = []
        if workspace_path:
            candidates.append(Path(workspace_path) / ".reports" / f"{run_id}.json")
        recorded_path = str(record.get("report_path") or "")
        if recorded_path:
            path = Path(recorded_path)
            json_path = path if path.suffix == ".json" else path.with_suffix(".json")
            if json_path.stem == run_id:
                candidates.append(json_path)
        for candidate in candidates:
            report = _read_json(candidate)
            if report:
                return report
        return {}

    @staticmethod
    def _issue_title(record: dict[str, Any], report: dict[str, Any]) -> str:
        """Read task-level title data without borrowing old run telemetry."""
        title = report.get("issue_title") or record.get("issue_title")
        if title:
            return str(title)
        recorded_path = str(record.get("report_path") or "")
        if not recorded_path:
            return ""
        path = Path(recorded_path)
        historical = _read_json(
            path if path.suffix == ".json" else path.with_suffix(".json")
        )
        return str(historical.get("issue_title") or "")

    def _issue_view(
        self,
        issue_id: str,
        record: dict[str, Any],
        report: dict[str, Any],
        observations: list[dict[str, Any]],
        now: float,
    ) -> dict[str, Any]:
        status = self._status(record.get("status"))
        created_at = _number(record.get("created_at"))
        updated_at = _number(record.get("updated_at"))
        workspace_path = str(record.get("workspace_path") or "")
        run_id = str(record.get("run_id") or "")
        workspace_short = self._short_workspace(workspace_path)
        execution = self._execution(record, report, observations, created_at, updated_at)
        conversation_id = record.get("conversation_id") or (run_id or None)
        run_metadata: dict[str, Any] = {}
        if conversation_id and run_id:
            try:
                manifest = ConversationStore().get(str(conversation_id))
                run_metadata = next(
                    (
                        item
                        for item in (manifest or {}).get("runs", [])
                        if item.get("run_id") == run_id
                    ),
                    {},
                )
            except Exception:
                run_metadata = {}
        for key in ("backend", "backend_session_id"):
            if not execution.get(key) and run_metadata.get(key) is not None:
                execution[key] = run_metadata[key]
        data_quality = self._data_quality(
            observations,
            created_at=_number(execution.get("started_at")) or created_at,
            updated_at=_number(execution.get("completed_at")) or updated_at,
        )
        attention = self._attention(
            status=status,
            record=record,
            idle_seconds=max(0, int(now - updated_at)) if updated_at else 0,
        )
        verification_status = record.get("verification_status")

        return {
            "issue_id": issue_id,
            "identifier": record.get("issue_identifier") or issue_id,
            "issue_title": self._issue_title(record, report),
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
            "verification_status": verification_status,
            "clarification_status": record.get("clarification_status"),
            "created_at": created_at,
            "updated_at": updated_at,
            "age_seconds": max(0, int(now - created_at)) if created_at else 0,
            "idle_seconds": max(0, int(now - updated_at)) if updated_at else 0,
            "run_id": run_id or None,
            "conversation_id": conversation_id,
            "parent_run_id": record.get("parent_run_id") or run_metadata.get("parent_run_id"),
            "stage_id": record.get("stage_id") or run_metadata.get("stage_id"),
            "branch_id": record.get("branch_id") or run_metadata.get("branch_id"),
            "run_turn_count": execution["turn_count"],
            "run_tool_count": execution["tool_count"],
            "run_output_len": execution["output_chars"],
            "run_cost_usd": execution["cost_usd"],
            "run_token_usage": execution["token_usage"],
            "run_workspace_dirty": execution["workspace_dirty"],
            "run_timeout_deadline_at": execution["timeout_deadline_at"],
            "report_status": report.get("status") or "",
            "output_excerpt": report.get("output_excerpt") or "",
            "tool_events_path": report.get("tool_events_path") or "",
            "verification_output": record.get("verification_output")
            or report.get("verification_output")
            or "",
            "session_end_reason": record.get("session_end_reason")
            or report.get("session_end_reason")
            or "",
            "session_end_summary": record.get("session_end_summary")
            or report.get("session_end_summary")
            or "",
            "run_last_event": record.get("run_last_event") or "",
            "run_last_tool": record.get("run_last_tool") or "",
            "collaboration_mode": record.get("collaboration_mode") or "",
            "mode_decision_reason": record.get("mode_decision_reason") or "",
            "debug_log_path": record.get("debug_log_path") or "",
            "pause_reason": record.get("pause_reason") or "",
            "has_conflict": bool(record.get("has_conflict")),
            "conflict_files": record.get("conflict_files") or [],
            "execution": execution,
            "data_quality": data_quality,
            "attention": attention,
            "display": {
                "task_state": status,
                "agent_state": self._agent_state(status, report, record),
                "verification_state": verification_status or (
                    "failed" if status == "verification_failed" else "not_run"
                ),
            },
        }

    def _short_workspace(self, workspace_path: str) -> str:
        if not workspace_path:
            return ""
        try:
            path = Path(workspace_path)
            return (
                "/" + str(path.relative_to(self.workspace))
                if self.workspace in path.parents
                else workspace_path
            )
        except (OSError, ValueError):
            return workspace_path

    @staticmethod
    def _agent_state(
        status: str, report: dict[str, Any], record: dict[str, Any]
    ) -> str:
        if report.get("status"):
            return str(report["status"])
        if not record.get("run_id"):
            return "not_started"
        if status == "running":
            return "running"
        if status in {"failed", "abandoned"}:
            return "failed" if status == "failed" else "stopped"
        if status in {"synced", "pending_review", "completed", "verification_failed"}:
            return "completed"
        return status

    @staticmethod
    def _execution(
        record: dict[str, Any],
        report: dict[str, Any],
        observations: list[dict[str, Any]],
        created_at: float,
        updated_at: float,
    ) -> dict[str, Any]:
        metrics: dict[str, Any] = {}
        for observation in observations:
            if observation.get("event_type") != "run_metrics":
                continue
            data = observation.get("data")
            if isinstance(data, dict):
                metrics.update(data)

        usage = record.get("run_token_usage") or report.get("token_usage") or {}
        metrics_usage = metrics.get("usage")
        if isinstance(metrics_usage, dict) and metrics_usage:
            usage = metrics_usage
        if not isinstance(usage, dict):
            usage = {}

        observed_tool_count = sum(
            observation.get("event_type") == "tool_call"
            for observation in observations
        )
        observed_turns = {
            int(turn)
            for observation in observations
            if isinstance(observation.get("data"), dict)
            and isinstance(
                turn := observation["data"].get("turn"), (int, float)
            )
            and not isinstance(turn, bool)
            and turn > 0
        }
        observed_turn_count = max(observed_turns, default=0)

        cost = _number(
            metrics.get(
                "total_cost_usd",
                record.get("run_cost_usd", report.get("cost_usd", 0.0)),
            )
        )
        duration_ms = metrics.get(
            "duration_ms",
            report.get("duration_ms", record.get("run_duration_ms")),
        )
        duration = _number(duration_ms, -1.0)
        registry_window_ms = (
            max(0.0, (updated_at - created_at) * 1000)
            if created_at and updated_at
            else None
        )
        return {
            "turn_count": max(
                int(record.get("run_turn_count") or 0),
                int(report.get("turn_count") or 0),
                observed_turn_count,
            ),
            "tool_count": max(
                int(record.get("run_tool_count") or 0),
                int(report.get("tool_count") or 0),
                observed_tool_count,
            ),
            "output_chars": int(record.get("run_output_len") or 0),
            "token_usage": usage,
            "cost_usd": cost,
            "duration_ms": duration if duration >= 0 else None,
            "registry_window_ms": registry_window_ms,
            "started_at": report.get("started_at")
            or record.get("run_started_at"),
            "completed_at": report.get("completed_at")
            or record.get("run_completed_at"),
            "backend": report.get("backend") or record.get("run_backend") or "",
            "backend_session_id": report.get("backend_session_id") or record.get("backend_session_id") or "",
            "runtime": report.get("runtime") or record.get("run_runtime") or "",
            "model": report.get("model") or record.get("run_model") or "",
            "workspace_dirty": record.get("run_workspace_dirty"),
            "timeout_deadline_at": record.get("run_timeout_deadline_at"),
            "changed_files": report.get("changed_files") or [],
            "diff_stats": report.get("diff_stats") or {},
        }

    @staticmethod
    def _data_quality(
        observations: list[dict[str, Any]], *, created_at: float, updated_at: float
    ) -> dict[str, Any]:
        source_times = [
            parsed
            for observation in observations
            if (parsed := _parse_timestamp(observation.get("source_ts"))) is not None
        ]
        event_span_ms = (
            (max(source_times) - min(source_times)) * 1000
            if len(source_times) > 1
            else None
        )
        registry_window_ms = (
            max(0.0, (updated_at - created_at) * 1000)
            if created_at and updated_at
            else None
        )
        explicit_quality = {
            str((observation.get("data") or {}).get("timestamp_quality") or "")
            for observation in observations
            if isinstance(observation.get("data"), dict)
        }
        if not observations:
            timing = "unavailable"
        elif len(source_times) < len(observations):
            timing = "partial"
        elif len(source_times) < 2:
            timing = "sparse"
        elif "arrival" in explicit_quality:
            timing = "arrival_timed"
        elif (
            registry_window_ms is not None
            and registry_window_ms >= 5000
            and event_span_ms is not None
            and event_span_ms < max(1000, registry_window_ms * 0.05)
        ):
            timing = "batch_captured"
        else:
            timing = "source_timed"
        return {
            "timestamp_quality": timing,
            "event_span_ms": event_span_ms,
            "registry_window_ms": registry_window_ms,
            "observations_visible": len(observations),
            "truncated_outputs": sum(
                bool((observation.get("data") or {}).get("content_truncated"))
                for observation in observations
                if isinstance(observation.get("data"), dict)
            ),
            "source": "registry + run report + transcript",
        }

    @staticmethod
    def _attention(
        *, status: str, record: dict[str, Any], idle_seconds: int
    ) -> dict[str, Any]:
        reason = ""
        action_id = ""
        action_label = ""
        tone = "warn"
        if record.get("has_conflict"):
            reason = "Git conflict"
            action_id, action_label, tone = "inspect_conflict", "Inspect conflict", "bad"
        elif record.get("pause_reason"):
            reason = str(record["pause_reason"])
            action_id, action_label = "resume_or_stop", "Resume or stop"
        elif record.get("clarification_status"):
            reason = f"Clarification: {record['clarification_status']}"
            action_id, action_label = "answer_clarification", "Answer clarification"
        elif status == "pending_review":
            reason = "Human review required"
            action_id, action_label = "review_run", "Review evidence"
        elif status == "verification_failed":
            reason = "Verification failed"
            action_id, action_label, tone = "inspect_verification", "Inspect failure", "bad"
        elif status in {"failed", "abandoned"}:
            reason = str(
                record.get("session_end_summary")
                or record.get("session_end_reason")
                or "Execution stopped"
            )
            action_id, action_label, tone = "inspect_run", "Inspect run", "bad"
        elif status in ACTIVE_STATUSES and idle_seconds > 300:
            reason = "No registry update for more than 5 minutes"
            action_id, action_label = "inspect_stall", "Inspect activity"
        return {
            "required": bool(action_id),
            "tone": tone if action_id else "",
            "reason": reason,
            "next_action": {
                "id": action_id,
                "label": action_label,
                "target": "run" if action_id else "",
            },
        }
