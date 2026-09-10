"""Application operations extracted from CLI; no argv or global stdio mutation."""

from __future__ import annotations

import argparse
import json
import logging
import os
import time
from pathlib import Path

from .models import CommandContext

logger = logging.getLogger(__name__)


def _find_metadata(
    context: CommandContext, args: argparse.Namespace
) -> tuple[Path | None, dict | None]:
    """Resolve orchestrator metadata.

    Returns (metadata_path, metadata_dict) or (None, None) if not found.
    """
    from orchestratord.workspace_locator import (
        _find_latest_metadata,
        get_workspace_root,
    )

    # 0. 多项目歧义检测：无显式参数且有多个存活项目时提示
    if not getattr(args, "workspace", None) and not getattr(args, "workflow", None):
        from orchestratord.workspace_locator import (
            get_live_projects,
        )

        live = get_live_projects()
        if len(live) > 1:
            subcmd = getattr(args, "server_subcommand", "server")
            context.multi_project_hint(live, f"orchestrator server {subcmd}")
            return None, None

    # Priority: explicit --workspace > --workflow > env var > latest metadata
    workspace_root = get_workspace_root(
        workspace_arg=getattr(args, "workspace", None),
        workflow_path=getattr(args, "workflow", None),
    )
    if workspace_root:
        # The daemon may have stored a relative workspace_root in
        # legacy metadata; compare resolved paths so `--workspace
        # ./workspace` matches "workspace" when CWDs align.
        workspace_root_str = str(workspace_root)

        def _same_root(stored: str | None) -> bool:
            if not stored:
                return False
            if stored == workspace_root_str:
                return True
            try:
                return Path(stored).resolve() == Path(workspace_root_str).resolve()
            except OSError:
                return False

        slug = _slug_from_workspace(context, workspace_root_str)
        metadata_path = context.metadata_directory / slug / "metadata.json"
        if metadata_path.exists():
            import json

            try:
                data = json.loads(metadata_path.read_text(encoding="utf-8"))
                return metadata_path, data
            except Exception:
                logger.debug("Command operation could not complete", exc_info=True)
        # Fallback: search by workspace_root matching
        if context.metadata_directory.exists():
            for md_dir in context.metadata_directory.iterdir():
                mf = md_dir / "metadata.json"
                if mf.exists():
                    import json

                    try:
                        data = json.loads(mf.read_text(encoding="utf-8"))
                        if _same_root(data.get("workspace_root")):
                            return mf, data
                    except Exception:
                        logger.debug(
                            "Command operation could not complete", exc_info=True
                        )

    # Fallback: latest metadata (only when no explicit --workspace/--workflow)
    has_explicit = getattr(args, "workspace", None) or getattr(args, "workflow", None)
    if not has_explicit:
        latest = _find_latest_metadata()
        if latest and latest.exists():
            import json

            try:
                data = json.loads(latest.read_text(encoding="utf-8"))
                return latest, data
            except Exception:
                logger.debug("Command operation could not complete", exc_info=True)

    return None, None


def _slug_from_workspace(context: CommandContext, ws_str: str) -> str:
    """Generate a deterministic slug from a workspace path string."""
    parts = [
        p
        for p in ws_str.strip().replace("/", "-").replace("\\", "-").split("-")
        if p and p not in ("tmp", ".orchestratord", "~")
    ]
    return "-".join(parts[-3:]) if parts else "default"


def _is_pid_alive(context: CommandContext, pid: int) -> bool:
    """Check whether a PID is alive, treating Linux zombies as stopped.

    Signal 0 reports zombies as alive, which can make ``server stop`` wait
    until its timeout for a daemon that has already exited.  Linux exposes
    the process state in ``/proc/<pid>/stat``; on other platforms we retain
    the signal-0 semantics.
    """
    try:
        os.kill(pid, 0)
    except (OSError, ProcessLookupError):
        return False
    try:
        with open(f"/proc/{pid}/stat", "rb") as f:
            # state is the field after the comm in parens; comm may contain
            # spaces, so take everything after the last ')'.
            state = f.read().rsplit(b")", 1)[1].split()[0]
        return state != b"Z"
    except (OSError, IndexError):
        return True


def _format_uptime(context: CommandContext, started_at: float) -> str:
    """Format uptime as human-readable string."""
    elapsed = time.time() - started_at
    if elapsed < 60:
        return f"{int(elapsed)}s"
    elif elapsed < 3600:
        return f"{int(elapsed / 60)}m {int(elapsed % 60)}s"
    else:
        hours = int(elapsed / 3600)
        minutes = int((elapsed % 3600) / 60)
        return f"{hours}h {minutes}m"


_REGISTRY_COUNT_ORDER = (
    "pending",
    "running",
    "pending_review",
    "completed",
    "verification_failed",
    "failed",
    "abandoned",
    "cancelled",
)


def _registry_counts_line(
    context: CommandContext, workspace_root: str | None
) -> str | None:
    """One-line issue tally from the workspace registry, for ``server status``.

    Pure read of ``<workspace_root>/.orchestratord_issue_registry.json``
    (flat ``{issue_id: record}``, lowercase status strings). Returns None
    when there is nothing trustworthy to show — no file, unreadable, or
    unexpected shape — so a registry problem never breaks ``status``.
    """
    if not workspace_root or workspace_root == "unknown":
        return None
    registry_path = Path(workspace_root) / ".orchestratord_issue_registry.json"
    if not registry_path.is_file():
        return None
    try:
        data = json.loads(registry_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    if not data:
        return "none registered"
    counts: dict[str, int] = {}
    for rec in data.values():
        status = (
            str(rec.get("status", "unknown")) if isinstance(rec, dict) else "unknown"
        )
        counts[status] = counts.get(status, 0) + 1
    parts = [f"{s}={counts[s]}" for s in _REGISTRY_COUNT_ORDER if counts.get(s)]
    parts += [
        f"{s}={n}" for s, n in sorted(counts.items()) if s not in _REGISTRY_COUNT_ORDER
    ]
    return " · ".join(parts)


def _runtime_lines(context: CommandContext, meta: dict) -> list[str]:
    """Render backend / agent / concurrency / API lines from launch context.

    Older daemons persist none of these fields; each line appears only
    when its source data exists, keeping ``status`` output backward
    compatible.
    """
    lines: list[str] = []
    backend = meta.get("backend")
    if backend:
        lines.append(f"  Backend        : {backend}")
    runtime = meta.get("runtime")
    if not isinstance(runtime, dict):
        runtime = {}
    agent_bits: list[str] = []
    provider_model = "/".join(
        str(p) for p in (runtime.get("provider"), runtime.get("model")) if p
    )
    if provider_model:
        agent_bits.append(provider_model)
    if runtime.get("permission_mode"):
        agent_bits.append(f"permission_mode={runtime['permission_mode']}")
    if runtime.get("approval_policy"):
        agent_bits.append(f"approval={runtime['approval_policy']}")
    if agent_bits:
        lines.append(f"  Agent          : {' · '.join(agent_bits)}")
    conc_bits: list[str] = []
    if runtime.get("max_concurrent_agents") is not None:
        conc_bits.append(f"{runtime['max_concurrent_agents']} concurrent agent(s)")
    if runtime.get("poll_interval_ms") is not None:
        conc_bits.append(f"poll every {runtime['poll_interval_ms']}ms")
    if conc_bits:
        lines.append(f"  Concurrency    : {' · '.join(conc_bits)}")
    api_port = meta.get("api_port")
    if api_port:
        lines.append(f"  API            : http://127.0.0.1:{api_port}")
    return lines


def _run_status(context: CommandContext, args: argparse.Namespace) -> int:
    """Show orchestrator daemon status. Idempotent — pure read."""
    meta_path, meta = _find_metadata(context, args)

    if meta is None:
        context.output.write("Orchestrator daemon: NOT RUNNING")
        context.output.write("  No orchestrator metadata found.")
        context.output.write(
            "  Hint: Start with 'orchestratord server start --workflow WORKFLOW.md'"
        )
        return 0  # idempotent: not-running is a valid status, not an error

    pid = meta.get("pid")
    started_at = meta.get("started_at", 0)
    project_slug = meta.get("project_slug", "unknown")
    workspace_root = meta.get("workspace_root", "unknown")
    workflow_path = meta.get("workflow_path")

    if pid and _is_pid_alive(context, pid):
        uptime = _format_uptime(context, started_at) if started_at else "unknown"
        context.output.write("Orchestrator daemon: RUNNING")
        context.output.write(f"  PID            : {pid}")
        context.output.write(f"  Uptime         : {uptime}")
        context.output.write(f"  Project        : {project_slug}")
        context.output.write(f"  Workspace root : {workspace_root}")
        if workflow_path:
            context.output.write(f"  Workflow       : {workflow_path}")
        for _line in _runtime_lines(context, meta):
            context.output.write(_line)
        counts_line = _registry_counts_line(context, workspace_root)
        if counts_line:
            context.output.write(f"  Issues         : {counts_line}")
        context.output.write(f"  Metadata       : {meta_path}")
    else:
        stale_age = _format_uptime(context, started_at) if started_at else "unknown"
        context.output.write(
            f"Orchestrator daemon: STOPPED (stale metadata from {stale_age} ago)"
        )
        context.output.write(f"  Project        : {project_slug}")
        context.output.write(f"  Workspace root : {workspace_root}")
        context.output.write(
            f"  Metadata       : {meta_path} (stale — clean up with 'server stop')"
        )
        # Auto-clean stale metadata
        if meta_path and meta_path.exists():
            meta_path.unlink()
            context.output.write("  -> Stale metadata cleaned up.")

    return 0
