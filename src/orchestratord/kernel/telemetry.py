"""Telemetry / metadata heartbeat — DESIGN §5 机制职责表第 6 行。

从 orchestrator.py 迁入的纯机制段：daemon session id 派生、
metadata.json 心跳重写、best-effort telemetry 汇总上报、关闭清理。
依赖全部为机制域（workspace_locator / api.runtime / telemetry.reporters）。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ..config.schema import WorkflowConfig

logger = logging.getLogger(__name__)


def derive_session_id(workspace_root: str | Path | None) -> str:
    """Stable session id for the orchestrator daemon.

    Combines the workspace root path with a daily salt so all
    orchestrator daemons on a given day share the same id
    (the polling loop is one continuous session for telemetry
    purposes — restart on a new day = new session).
    """
    try:
        from datetime import datetime, timezone
        import hashlib

        workspace = str(workspace_root) if workspace_root else ""
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        raw = f"orchestrator:{workspace}:{day}"
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]
    except Exception:
        return "orchestrator"


def report_telemetry(workflow: WorkflowConfig) -> None:
    """Best-effort: push today's telemetry summary to the remote issue.

    Only when ``workflow.telemetry.reporting_enabled`` is set; the
    api_key falls back to the tracker's GitCode token. Never raises.
    """
    try:
        tele = getattr(workflow, "telemetry", None)
        if tele is None or not tele.reporting_enabled:
            return
        tracker = getattr(workflow, "tracker", None)
        api_key = tele.api_key or (getattr(tracker, "api_key", "") or "")
        if not api_key:
            return
        from orchestratord.telemetry.reporters import report_day

        report_day(
            owner=tele.report_owner or getattr(tracker, "owner", "") or "",
            repo=tele.report_repo or getattr(tracker, "repo", "") or "",
            api_key=api_key,
            title=tele.issue_title,
            force=True,
        )
    except Exception:
        logger.debug("telemetry report skipped", exc_info=True)


def metadata_extras(workflow: WorkflowConfig, agent_runner: Any, backend: Any) -> dict:
    """Launch-context fields persisted into metadata.json.

    The heartbeat loop rewrites metadata every 30s, so extras must be
    supplied on EVERY write — omitting them here would wipe the
    backend/runtime fields within one heartbeat interval.
    """
    agent_cfg = getattr(workflow, "agent", None)
    sandbox_cfg = getattr(workflow, "sandbox", None)
    approval = getattr(sandbox_cfg, "approval_policy", None)
    runtime = {
        "provider": getattr(agent_cfg, "provider", None),
        "model": getattr(agent_cfg, "model", None),
        "permission_mode": getattr(agent_cfg, "permission_mode", None),
        "max_concurrent_agents": getattr(
            agent_cfg, "max_concurrent_agents", None
        ),
        "poll_interval_ms": getattr(
            getattr(workflow, "polling", None), "interval_ms", None
        ),
        # A structured dict is the sandbox default; the resolved
        # auto-approve/ask behavior is what approval_policy resolves to
        # at run time — status only shows the configured form.
        "approval_policy": "structured" if isinstance(approval, dict) else approval,
    }
    backend_name = (
        getattr(agent_runner, "backend_name", None)
        or getattr(backend, "name", None)
    )
    extras: dict = {"backend_name": backend_name, "runtime": runtime}
    # Local import: the API layer is optional in daemon-free tooling.
    from ..api.runtime import get_api_port

    api_port = get_api_port()
    if api_port is not None:
        extras["api_port"] = api_port
    return extras


async def metadata_heartbeat_loop(
    *,
    workspace_root: str | Path | None,
    workflow_path: str | None,
    started_at: Any,
    shutdown_event: asyncio.Event,
    extras_provider: Callable[[], dict],
) -> None:
    """Periodically rewrite metadata so CLI can always discover the orchestrator.

    If metadata.json is accidentally deleted, this recreates it within
    the heartbeat interval (30s), preventing the ``server start`` PID
    guard from being bypassed for a running instance.
    """
    from ..workspace_locator import write_orchestrator_metadata

    while not shutdown_event.is_set():
        try:
            await asyncio.wait_for(
                shutdown_event.wait(),
                timeout=30.0,
            )
            break  # shutdown requested
        except asyncio.TimeoutError:
            pass

        write_orchestrator_metadata(
            workspace_root=workspace_root,
            workflow_path=workflow_path,
            started_at=started_at,
            **extras_provider(),
        )


def shutdown_cleanup(workspace_root: str | Path | None) -> None:
    """Clear orchestrator metadata on graceful shutdown."""
    from ..workspace_locator import clear_orchestrator_metadata

    clear_orchestrator_metadata(workspace_root)
