"""Application operations extracted from CLI; no argv or global stdio mutation."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .models import CommandContext

if TYPE_CHECKING:
    from orchestratord.issue_registry.models import IssueRecord
_PR_URL_RE = re.compile(r"^(?P<host>https?://[^/]+)/(?P<owner>[^/]+)/(?P<repo>[^/]+)/")

logger = logging.getLogger(__name__)


def _control_path(
    context: CommandContext, workspace_root: str | Path | None = None
) -> Path:
    """Path to the orchestrator control directory.

    Uses the workspace root when provided (preferred), otherwise falls
    back to the ORCHESTRATORD_WORKSPACE_ROOT env var or ~/.orchestratord.
    """
    workspace_root = workspace_root or context.workspace_root
    if workspace_root is not None:
        return Path(workspace_root) / ".orchestrator_control"
    base = Path(
        os.environ.get("ORCHESTRATORD_WORKSPACE_ROOT", Path.home() / ".orchestratord")
    )
    return base / ".orchestrator_control"


def _resolve_sock_path(
    context: CommandContext,
    issue_id: str,
    workspace_root: str | Path | None = None,
) -> Path | None:
    """Resolve the control socket path for an issue via the registry."""
    try:
        ws = (
            Path(workspace_root or context.workspace_root)
            if (workspace_root or context.workspace_root)
            else None
        )
        if ws is None:
            from orchestratord.workspace_locator import get_registry_path

            registry_path = get_registry_path()
        else:
            registry_path = ws / ".orchestratord_issue_registry.json"
        if registry_path is None or not registry_path.exists():
            return None

        registry = context.registry(registry_path)
        record = registry.get(issue_id) or registry.get_by_identifier(issue_id)
        if record is None or not record.run_id or not record.workspace_path:
            return None
        sock_path = (
            Path(record.workspace_path) / ".run_control" / f"{record.run_id}.sock"
        )
        return sock_path if sock_path.exists() else None
    except Exception:
        logger.debug("Command operation could not complete", exc_info=True)
        return None


async def _send_and_wait(
    context: CommandContext,
    sock_path: Path,
    cmd: str,
    payload: str,
    expected_type: str,
    timeout: float = 30.0,
) -> dict | None:
    """Send a control command via socket and wait for a confirmation event.

    Opens a Unix socket connection, sends the command, then keeps the
    connection open reading event lines until one matching
    ``expected_type`` arrives. Returns the event's ``data`` dict, or
    ``None`` on timeout.
    """
    import json as _json

    reader, writer = await asyncio.open_unix_connection(str(sock_path))
    started = asyncio.get_event_loop().time()
    try:
        # Send the command.
        writer.write(
            (_json.dumps({"cmd": cmd, "payload": payload}) + "\n").encode("utf-8"),
        )
        await writer.drain()

        # Listen for the confirmation event.
        while True:
            remaining = timeout - (asyncio.get_event_loop().time() - started)
            if remaining <= 0:
                return None
            try:
                line = await asyncio.wait_for(reader.readline(), timeout=remaining)
            except TimeoutError:
                return None
            if not line:
                return None  # socket closed
            try:
                event = _json.loads(line.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            if event.get("type") == expected_type:
                return event.get("data", {})
            # Ignore other event types (TextDelta, ToolCallEvent, etc.)
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            logger.debug("Command operation could not complete", exc_info=True)


async def _write_control(
    context: CommandContext,
    cmd: str,
    issue_id: str,
    extra: str = "",
    workspace_root: str | Path | None = None,
) -> int:
    """Send a control command, preferring the Unix socket for near-real-time
    delivery. Falls back to the control-file mechanism (picked up on the
    orchestrator's next poll cycle) when the socket is unavailable.

    Socket-first delivery eliminates the 30s poll-cycle
    latency for ``pause`` / ``resume`` / ``stop`` when the agent is
    running and the control socket is alive.
    """
    from pathlib import Path

    if context.runtime is not None and cmd in {"pause", "resume", "stop"}:
        registry = context.runtime._registry
        record = registry.get(issue_id) or registry.get_by_identifier(issue_id)
        resolved_id = record.issue_id if record else issue_id
        if resolved_id not in context.runtime._state.running:
            context.output.write(
                f"error: issue {issue_id} has no running session", error=True
            )
            return 1
        context.runtime._apply_control_command(cmd, resolved_id, extra)
        context.output.write(
            f"Control command '{cmd}' sent for issue {resolved_id} (in-process)"
        )
        return 0

    # Try to resolve run_id + workspace_path from the
    # registry so we can attempt a direct socket connection.
    # Only agent-level commands (pause/resume/stop) go through the
    # socket; orchestrator-level commands (retry/rebase/etc.) always
    # go through the control file.
    _SOCKET_CMDS = {"pause", "resume", "stop"}
    if cmd in _SOCKET_CMDS and workspace_root is not None:
        try:
            registry_path = Path(workspace_root) / ".orchestratord_issue_registry.json"
            if registry_path.exists():
                registry = context.registry(registry_path)
                record = registry.get(issue_id) or registry.get_by_identifier(issue_id)
                if record is not None and record.run_id and record.workspace_path:
                    sock_path = (
                        Path(record.workspace_path)
                        / ".run_control"
                        / f"{record.run_id}.sock"
                    )
                    if sock_path.exists():
                        import asyncio as _asyncio
                        import json as _json

                        async def _send_via_socket() -> None:
                            _reader, writer = await _asyncio.open_unix_connection(
                                str(sock_path),
                            )
                            try:
                                payload = {"cmd": cmd, "payload": extra}
                                writer.write(
                                    (_json.dumps(payload) + "\n").encode("utf-8"),
                                )
                                await writer.drain()
                            finally:
                                writer.close()
                                try:
                                    await writer.wait_closed()
                                except Exception:
                                    logger.debug(
                                        "Command operation could not complete",
                                        exc_info=True,
                                    )

                        await _send_via_socket()
                        context.output.write(
                            f"Control command '{cmd}' sent for issue {issue_id} (via socket)"
                        )
                        context.output.write(
                            "  The agent will process this at the next tool-result boundary."
                        )
                        return 0
        except Exception:
            logger.debug("Command operation could not complete", exc_info=True)
            # Fall through to control-file path.

    # Fallback: write a control file for the orchestrator's next poll.
    control_dir = _control_path(context, workspace_root=workspace_root)
    control_dir.mkdir(parents=True, exist_ok=True)

    control_file = control_dir / f"{cmd}_{issue_id}.control"
    payload = f"{cmd}\n{issue_id}\n{extra}\n"
    try:
        control_file.write_text(payload, encoding="utf-8")
        context.output.write(
            f"Control command '{cmd}' sent for issue {issue_id} (via control file)"
        )
        context.output.write(
            "  The orchestrator will pick this up on its next poll cycle."
        )
        return 0
    except Exception as exc:
        logger.debug("Command operation could not complete", exc_info=True)
        context.output.write(
            f"Failed to send '{cmd}' for issue {issue_id}: {exc}", error=True
        )
        return 1


async def _try_socket_inject(context: CommandContext, issue_id: str, hint: str) -> bool:
    """Try to send an inject command via the control socket.

    Returns ``True`` if the hint was queued via the socket (which
    routes to ``queue_pending_message`` for real-time delivery at
    the next ToolResult boundary). Returns ``False`` if the socket
    is unavailable — the caller should fall back to file-based inject.

    CLI ``issue inject`` prefers socket delivery for
    near-real-time inject, matching the socket ``inject`` command.
    """
    try:
        from orchestratord.workspace_locator import get_registry_path

        registry_path = get_registry_path(workspace_arg=context.workspace_root)
        if registry_path is None or not registry_path.exists():
            return False

        registry = context.registry(registry_path)
        record = registry.get(issue_id) or registry.get_by_identifier(issue_id)
        if record is None or not record.run_id or not record.workspace_path:
            return False
        sock_path = (
            Path(record.workspace_path) / ".run_control" / f"{record.run_id}.sock"
        )
        if not sock_path.exists():
            return False
        import asyncio as _asyncio
        import json as _json

        async def _send() -> None:
            _reader, writer = await _asyncio.open_unix_connection(str(sock_path))
            try:
                writer.write(
                    (_json.dumps({"cmd": "inject", "payload": hint}) + "\n").encode(
                        "utf-8"
                    ),
                )
                await writer.drain()
            finally:
                writer.close()
                try:
                    await writer.wait_closed()
                except Exception:
                    logger.debug("Command operation could not complete", exc_info=True)

        await _send()
        return True
    except Exception:
        logger.debug("Command operation could not complete", exc_info=True)
        return False


def _run_list(
    context: CommandContext, registry_path: Path | None, args: argparse.Namespace
) -> int:
    """List all issues with status. Idempotent — pure read."""
    if not registry_path or not registry_path.exists():
        ws = getattr(args, "workspace", None)
        wf = getattr(args, "workflow", None)

        # 当 --workspace / --workflow 都没传时，检查是否有多个活跃 orch 项目
        if not ws and not wf:
            from orchestratord.workspace_locator import (
                get_live_projects,
            )

            live = get_live_projects()
            if len(live) > 1:
                context.multi_project_hint(live, "orchestrator issue list")
                return 0

        from orchestratord.workspace_locator import (
            get_workspace_root,
            list_orchestrator_projects,
        )

        workspace_root = get_workspace_root(workspace_arg=ws, workflow_path=wf)
        projects = list_orchestrator_projects()

        if workspace_root and projects:
            p = projects[0]
            pid = p.get("pid", "?")
            context.output.write(
                f"Orchestrator is running (PID {pid}, {p.get('project_slug', '?')})"
            )
            context.output.write(f"Workspace: {workspace_root}")
            context.output.write("No issues processed yet.")
        else:
            context.output.write("No orchestrator registry found. No issues to list.")
            context.output.write(
                "Hint: Start with 'orchestratord server start --workflow WORKFLOW.md'"
            )
        return 0  # idempotent: no-issues is a valid state

    registry = context.registry(registry_path)
    counts: dict[str, int] = {
        "PENDING": 0,
        "RUNNING": 0,
        "SYNCED": 0,
        "COMPLETED": 0,
        "FAILED": 0,
        "ABANDONED": 0,
    }
    records = list(registry._records.values())

    # Filter by status
    status_filter = getattr(args, "status", None)
    if status_filter:
        records = [
            r for r in records if _get_status_str(context, r.status) == status_filter
        ]

    if not records:
        context.output.write("No issues found.")
        if status_filter:
            context.output.write(f"  (filtered by status: {status_filter})")
        return 0

    # Status display mapping matching README Demo format
    _STATUS_DISPLAY = {
        "completed": "done",
        "pending_review": "paused",
        "running": "running",
        "pending": "pending",
        "synced": "synced",
        "failed": "failed",
        "abandoned": "abandoned",
        "verification_failed": "vfailed",
    }

    context.output.write(f"{'ID':<20} {'STATUS':<10} {'BRANCH':<25} {'ATTEMPTS':<9} PR")
    for r in records:
        raw_status = _get_status_str(context, r.status)
        display_status = _STATUS_DISPLAY.get(raw_status, raw_status)
        branch = r.branch_name or "-"
        attempts = str(r.attempt_count) if r.attempt_count else "-"
        pr = r.pr_url or "-"
        context.output.write(
            f"{r.issue_id:<20} {display_status:<10} {branch:<25} {attempts:<9} {pr}"
        )

    context.output.write()
    for r in records:
        s = _get_status_str(context, r.status)
        counts[s.upper()] = counts.get(s.upper(), 0) + 1
    context.output.write(f"  PENDING  : {counts.get('PENDING', 0)}")
    context.output.write(f"  RUNNING  : {counts.get('RUNNING', 0)}")
    context.output.write(f"  SYNCED   : {counts.get('SYNCED', 0)}")
    context.output.write(f"  COMPLETED: {counts.get('COMPLETED', 0)}")
    context.output.write(f"  FAILED   : {counts.get('FAILED', 0)}")
    context.output.write(f"  ABANDONED: {counts.get('ABANDONED', 0)}")
    return 0


def _run_show(
    context: CommandContext, registry_path: Path | None, args: argparse.Namespace
) -> int:
    """Show details for a specific issue. Idempotent — pure read."""
    issue_id = getattr(args, "id", None) or getattr(args, "issue_id", None)
    if not issue_id:
        context.output.write("error: --id is required", error=True)
        return 2

    if not registry_path or not registry_path.exists():
        context.output.write(
            f"No registry found. Cannot show issue {issue_id}.", error=True
        )
        return 1

    registry = context.registry(registry_path)
    record = registry.get_by_issue_ref(issue_id)
    if record is None:
        context.output.write(f"Issue {issue_id} not found in registry.", error=True)
        return 1

    created = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(record.created_at))
    updated = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(record.updated_at))

    context.output.write(f"Issue: {record.issue_id}")
    context.output.write(f"  Identifier     : {record.issue_identifier}")
    context.output.write(f"  Status         : {record.status.value}")
    context.output.write(f"  Branch         : {record.branch_name or '-'}")
    context.output.write(f"  Base Branch    : {record.base_branch or 'main'}")
    context.output.write(f"  Commit SHA     : {record.commit_sha or '-'}")
    context.output.write(f"  PR Number      : {record.pr_number or '-'}")
    context.output.write(f"  PR URL         : {record.pr_url or '-'}")
    pr_created = getattr(record, "pr_created_at", None)
    if pr_created:
        pr_created_text = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(pr_created))
        # Time from issue claim to first PR creation — the orchestrator's
        # key "issue → PR" latency metric for leadership reporting.
        latency_s = pr_created - record.created_at
        context.output.write(f"  PR Created     : {pr_created_text}")
        context.output.write(f"  Issue→PR Time  : {latency_s:.0f}s")
    else:
        context.output.write("  PR Created     : -")
    context.output.write(f"  Attempts       : {record.attempt_count}")
    context.output.write(f"  Run ID         : {getattr(record, 'run_id', None) or '-'}")
    context.output.write(
        f"  Turns / Tools  : {getattr(record, 'run_turn_count', 0)} / {getattr(record, 'run_tool_count', 0)}"
    )
    context.output.write(
        f"  Last Event     : {getattr(record, 'run_last_event', None) or '-'}"
    )
    context.output.write(
        f"  Last Tool      : {getattr(record, 'run_last_tool', None) or '-'}"
    )
    context.output.write(f"  Output Chars   : {getattr(record, 'run_output_len', 0)}")
    deadline = getattr(record, "run_timeout_deadline_at", None)
    if deadline:
        deadline_text = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(deadline))
    else:
        deadline_text = "-"
    context.output.write(f"  Timeout By     : {deadline_text}")
    workspace_dirty = getattr(record, "run_workspace_dirty", None)
    dirty_text = "-" if workspace_dirty is None else str(workspace_dirty).lower()
    context.output.write(f"  Workspace Dirty: {dirty_text}")
    context.output.write(f"  Workspace Path : {record.workspace_path or '-'}")
    context.output.write(
        f"  Debug Log      : {getattr(record, 'debug_log_path', None) or '-'}"
    )
    context.output.write(f"  Created        : {created}")
    context.output.write(f"  Updated        : {updated}")
    if record.clarification_status:
        context.output.write(f"  Clarification  : {record.clarification_status}")
    _print_session_usage(context, record)
    return 0


def _print_session_usage(context: CommandContext, record: IssueRecord) -> None:
    """Print token/cost usage for the issue, aggregating all runs.

    Session snapshots are written by AgentRunner to
    ``~/.orchestratord/sessions/<run_id>/session.json``; the JSONL telemetry
    events carry the same usage data when telemetry is enabled. Aggregates
    every run of the issue (``previous_run_ids`` + current ``run_id``) and
    prints the total, then the most recent run's per-model detail. Pure
    read: missing/unreadable snapshots are skipped silently.
    """
    import json

    run_ids: list[str] = []
    prev = getattr(record, "previous_run_ids", None) or []
    if isinstance(prev, (list, tuple)):
        run_ids.extend(str(r) for r in prev)
    run_id = getattr(record, "run_id", None)
    if run_id and str(run_id) not in run_ids:
        run_ids.append(str(run_id))

    totals: dict[str, float] = {
        "input_tokens": 0.0,
        "output_tokens": 0.0,
        "cache_creation_input_tokens": 0.0,
        "cache_read_input_tokens": 0.0,
        "cost_usd": 0.0,
    }
    last_detail: str | None = None
    for rid in run_ids:
        try:
            snapshot_path = (
                Path.home() / ".orchestratord" / "sessions" / rid / "session.json"
            )
            if not snapshot_path.exists():
                continue
            data = json.loads(snapshot_path.read_text(encoding="utf-8"))
            model = data.get("model", "")
            cost_block = data.get("cost") or {}
            usage = (
                cost_block.get("model_usage") if isinstance(cost_block, dict) else None
            )
            if not usage:
                usage = data.get("model_usage") or {}
            if isinstance(usage, dict) and usage:
                for m, u in usage.items():
                    for key in totals:
                        totals[key] += float(u.get(key, 0) or 0)
                    last_detail = (
                        f"model={m} input={u.get('input_tokens', 0)} "
                        f"output={u.get('output_tokens', 0)} "
                        f"cache_in={u.get('cache_creation_input_tokens', 0)} "
                        f"cache_read={u.get('cache_read_input_tokens', 0)} "
                        f"cost=${float(u.get('cost_usd', 0) or 0):.4f}"
                    )
            else:
                total = cost_block.get("total_cost_usd", 0.0)
                if total:
                    totals["cost_usd"] += float(total)
                    last_detail = f"model={model or '-'} total_cost=${float(total):.4f}"
        except Exception:
            logger.debug("Command operation could not complete", exc_info=True)
            continue  # Skip unreadable snapshots; never fail issue show.

    if (
        not totals["input_tokens"]
        and not totals["output_tokens"]
        and not totals["cost_usd"]
    ):
        # Session snapshots (claude/codex style) carry the usage; backends
        # that report through SESSION_COMPLETE payloads (dsh, opencode)
        # persist it on the registry record instead — fall back there.
        registry_usage = getattr(record, "run_token_usage", None)
        if isinstance(registry_usage, dict) and registry_usage:
            context.output.write(
                f"  Usage (last)   : input={registry_usage.get('input', 0)} "
                f"output={registry_usage.get('output', 0)} "
                f"reasoning={registry_usage.get('reasoning', 0)} "
                f"cache_read={registry_usage.get('cache_read', 0)} "
                "(cost not reported by backend)"
            )
        return
    context.output.write(
        f"  Usage (total)  : runs={len(run_ids)} input={totals['input_tokens']:.0f} "
        f"output={totals['output_tokens']:.0f} "
        f"cache_in={totals['cache_creation_input_tokens']:.0f} "
        f"cache_read={totals['cache_read_input_tokens']:.0f} "
        f"cost=${totals['cost_usd']:.4f}"
    )
    if last_detail:
        context.output.write(f"  Usage (last)   : {last_detail}")


def _resolve_issue_workspace_path(
    context: CommandContext,
    issue_id: str,
    workspace_arg: str | None = None,
) -> Path | None:
    """Resolve an issue workspace, including sequential registry layouts."""
    from orchestratord.workspace_locator import get_registry_path, get_workspace_root

    workspace_root = get_workspace_root(
        workspace_arg=workspace_arg
        or context.workspace_root
        or os.environ.get("ORCHESTRATORD_WORKSPACE_ROOT")
    )
    registry_path = (
        get_registry_path(workspace_arg=str(workspace_root)) if workspace_root else None
    )
    if registry_path and registry_path.exists():
        try:
            registry = context.registry(registry_path)
            record = registry.get_by_issue_ref(issue_id)
            if record:
                root = Path(record.workspace_path or workspace_root)
                candidates = []
                identifier = record.issue_identifier
                if identifier:
                    candidates.append(root / identifier)
                candidates.append(root)
                for candidate in candidates:
                    if candidate.exists():
                        return candidate
        except Exception:
            logger.debug("Command operation could not complete", exc_info=True)

    base = workspace_root or Path.home() / ".orchestratord" / "workspace"
    if not base.exists():
        return None
    for wd in base.iterdir():
        if not wd.is_dir():
            continue
        metadata_file = wd / ".metadata"
        if metadata_file.exists():
            import json

            try:
                metadata = json.loads(metadata_file.read_text(encoding="utf-8"))
                if metadata.get("issue_id") == issue_id:
                    return wd
            except Exception:
                logger.debug("Command operation could not complete", exc_info=True)
        if wd.name == issue_id or issue_id in wd.name:
            return wd
    return None


async def _run_stop(
    context: CommandContext,
    args: argparse.Namespace,
    registry_path: Path | None = None,
    workspace_root: str | Path | None = None,
) -> int:
    """Stop a running issue agent. Idempotent — already-stopped → success."""
    issue_id = getattr(args, "id", None)
    if not issue_id:
        context.output.write("error: --id is required", error=True)
        return 2

    skip_confirm = getattr(args, "yes", False)

    # Check registry for current status (best-effort)
    current_status = None
    if registry_path and registry_path.exists():
        try:
            registry = context.registry(registry_path)
            record = registry.get(issue_id) or registry.get_by_identifier(issue_id)
            if record is not None:
                current_status = record.status.value
        except Exception as exc:
            logger.debug("Command operation could not complete", exc_info=True)
            context.output.write(f"Warning: could not read registry: {exc}", error=True)

    if current_status is not None:
        # RUNNING is the only status where the stop command will be effective.
        # For all other statuses the orchestrator cannot find the issue in
        # _state.running and the control file will be silently ignored.
        if current_status != "running":
            context.output.write(
                f"Warning: issue {issue_id} is not currently running (status: {current_status}).",
                error=True,
            )
            context.output.write(
                "  The stop command will not take effect — no agent session to stop.",
                error=True,
            )
            if not skip_confirm:
                try:
                    raw = context.confirm("  Write control file anyway? [y/N]: ")
                    if raw.strip().lower() not in ("y", "yes"):
                        context.output.write("Stop cancelled.")
                        return 0
                except (EOFError, KeyboardInterrupt):
                    context.output.write("\nStop cancelled.")
                    return 0
            else:
                context.output.write("  (use --id to target a running issue)")
    else:
        context.output.write(
            f"Warning: issue {issue_id} not found in registry — cannot verify current status.",
            error=True,
        )
        if not skip_confirm:
            try:
                raw = context.confirm("  Write stop control file anyway? [y/N]: ")
                if raw.strip().lower() not in ("y", "yes"):
                    context.output.write("Stop cancelled.")
                    return 0
            except (EOFError, KeyboardInterrupt):
                context.output.write("\nStop cancelled.")
                return 0

    # Confirmation prompt (unless --yes is set)
    if not skip_confirm:
        try:
            raw = context.confirm(f"Stop agent for issue {issue_id}? [y/N]: ")
            if raw.strip().lower() not in ("y", "yes"):
                context.output.write("Stop cancelled.")
                return 0
        except (EOFError, KeyboardInterrupt):
            context.output.write("\nStop cancelled.")
            return 0

    context.output.write(f"Issue stop: sending stop command for {issue_id}")
    no_wait = getattr(args, "no_wait", False)

    sock_path = _resolve_sock_path(context, issue_id, workspace_root)
    if sock_path is not None and not no_wait:

        async def _do_stop() -> int:
            t0 = asyncio.get_event_loop().time()
            data = await _send_and_wait(
                context, sock_path, "stop", "", "SessionComplete", timeout=10.0
            )
            elapsed = asyncio.get_event_loop().time() - t0
            if data is not None:
                context.output.write(f"Agent stopped ({elapsed:.1f}s).")
                return 0
            else:
                context.output.write(
                    "Stop sent. Agent is unwinding "
                    "(may take a few seconds for long-running tools)."
                )
                return 0

        return await _do_stop()
    else:
        return await _write_control(
            context, "stop", issue_id, workspace_root=workspace_root
        )


async def _run_pause(
    context: CommandContext,
    args: argparse.Namespace,
    workspace_root: str | Path | None = None,
) -> int:
    """Pause a running issue agent. Idempotent — already-paused → success."""
    issue_id = getattr(args, "id", None)
    if not issue_id:
        context.output.write("error: --id is required", error=True)
        return 2
    reason = getattr(args, "reason", "") or "operator requested pause"
    no_wait = getattr(args, "no_wait", False)

    sock_path = _resolve_sock_path(context, issue_id, workspace_root)
    if sock_path is not None and not no_wait:

        async def _do_pause() -> int:
            t0 = asyncio.get_event_loop().time()
            data = await _send_and_wait(
                context, sock_path, "pause", reason, "Paused", timeout=30.0
            )
            elapsed = asyncio.get_event_loop().time() - t0
            if data is not None:
                turn = data.get("turn", "?")
                tool = data.get("tool_name", "?")
                context.output.write(
                    f"Agent paused at turn {turn}, tool {tool!r} ({elapsed:.1f}s)."
                )
                return 0
            else:
                context.output.write(
                    "Pause acknowledged but agent is in a long operation "
                    "(30s timeout). It will pause at the next tool boundary."
                )
                return 0

        return await _do_pause()
    elif sock_path is not None and no_wait:
        # Fire and forget via socket.
        return await _write_control(
            context, "pause", issue_id, reason, workspace_root=workspace_root
        )
    else:
        context.output.write(f"Issue pause: sending pause command for {issue_id}")
        return await _write_control(
            context, "pause", issue_id, reason, workspace_root=workspace_root
        )


async def _run_resume(
    context: CommandContext,
    args: argparse.Namespace,
    workspace_root: str | Path | None = None,
) -> int:
    """Resume a paused issue agent. Idempotent — running → success."""
    issue_id = getattr(args, "id", None)
    if not issue_id:
        context.output.write("error: --id is required", error=True)
        return 2
    no_wait = getattr(args, "no_wait", False)

    sock_path = _resolve_sock_path(context, issue_id, workspace_root)
    if sock_path is not None and not no_wait:

        async def _do_resume() -> int:
            t0 = asyncio.get_event_loop().time()
            data = await _send_and_wait(
                context, sock_path, "resume", "", "Resumed", timeout=5.0
            )
            elapsed = asyncio.get_event_loop().time() - t0
            if data is not None:
                context.output.write(f"Agent resumed ({elapsed:.1f}s).")
                return 0
            else:
                context.output.write(
                    "Resume sent but no confirmation (5s). The agent may already be running."
                )
                return 0

        return await _do_resume()
    elif sock_path is not None and no_wait:
        return await _write_control(
            context, "resume", issue_id, workspace_root=workspace_root
        )
    else:
        context.output.write(f"Issue resume: sending resume command for {issue_id}")
        return await _write_control(
            context, "resume", issue_id, workspace_root=workspace_root
        )


def _run_clarify(
    context: CommandContext,
    args: argparse.Namespace,
    *,
    registry_path: Path | None = None,
    workspace_root: Path | None = None,
) -> int:
    """Answer a clarification request. Idempotent — re-answering updates in place."""
    issue_id = getattr(args, "id", None)
    list_clarifications = bool(getattr(args, "list_clarifications", False))
    if not issue_id and not list_clarifications:
        context.output.write("error: --id is required", error=True)
        return 2

    answer = getattr(args, "answer", None)
    forward = getattr(args, "forward_to_author", False)

    recheck = bool(getattr(args, "recheck", False))
    resolve = bool(getattr(args, "resolve", False))
    if (
        not answer
        and not forward
        and not list_clarifications
        and not recheck
        and not resolve
    ):
        context.output.write(
            "error: --answer is required unless --forward-to-author is used", error=True
        )
        return 2

    queue_path = (
        Path(workspace_root) / ".orchestratord_clarification_queue.json"
        if workspace_root is not None
        else None
    )
    queue = context.clarification_queue(queue_path)

    if list_clarifications:
        items = queue.list_items()
        if not items:
            context.output.write("No clarification records.")
            return 0
        for item in items:
            context.output.write(
                f"{item.issue_id}\t{item.status.value}\t{item.question}"
            )
        return 0

    registry = context.registry(registry_path) if registry_path is not None else None
    if recheck:
        queue.remove(issue_id)
        record = registry.get(issue_id) if registry is not None else None
        if record is None:
            context.output.write(
                f"Issue {issue_id} is not present in the registry.", error=True
            )
            return 1
        record.clarification_status = None
        record.open_questions = []
        record.clarification_round = 0
        record.clarifier_fingerprint = None
        record.clarification_replies = []
        record.local_answer = None
        record.local_answer_source = None
        record.touch()
        registry._save()
        context.output.write(
            f"Issue {issue_id} will be rechecked on the next poll cycle."
        )
        return 0

    if resolve:
        queue.remove(issue_id)
        if registry is None:
            context.output.write("Could not locate the issue registry.", error=True)
            return 1
        record = registry.get(issue_id)
        if record is None:
            context.output.write(
                f"Issue {issue_id} is not present in the registry.", error=True
            )
            return 1
        registry.mark_clarification_resolved(
            issue_id,
            fingerprint=record.clarifier_fingerprint or "manual",
            answer=answer or "Manually resolved by operator",
            source="operator",
            status="manual_resolved",
        )
        context.output.write(f"Issue {issue_id} clarification marked resolved.")
        return 0

    if forward:
        item = queue.mark_awaiting_author(issue_id)
        if item is None:
            context.output.write(
                f"No pending clarification for issue {issue_id}.", error=True
            )
            return 1
        context.output.write(f"Issue {issue_id} marked for author clarification.")
        return 0

    resolved = queue.resolve(issue_id, answer or "", source="clarification_queue")
    if resolved is None:
        context.output.write(
            f"Failed to write answer for issue {issue_id}.", error=True
        )
        return 1

    context.output.write(
        f"Answer recorded for issue {issue_id}: {answer or '(forwarded to author)'}"
    )
    context.output.write(f"Status: {resolved.status.value}")
    context.output.write("The orchestrator will pick this up on its next poll cycle.")
    return 0


async def _run_inject(context: CommandContext, args: argparse.Namespace) -> int:
    """Inject operator hints. Idempotent — listing/removal are safe."""
    issue_id = getattr(args, "id", None)
    if not issue_id:
        context.output.write("error: --id is required", error=True)
        return 2

    ws_path = _resolve_issue_workspace_path(
        context, issue_id, workspace_arg=getattr(args, "workspace", None)
    )
    hints_file = ws_path / ".operator_hints.md" if ws_path else None
    if hints_file is None:
        context.output.write(
            f"Could not find workspace for issue {issue_id}.\n"
            "Hints are stored in the issue's workspace directory.\n"
            "Set ORCHESTRATORD_WORKSPACE_ROOT or run the orchestrator with --workflow.",
            error=True,
        )
        return 1

    hint = getattr(args, "hint", None)
    hint_flag = getattr(args, "hint_flag", None)
    if hint and hint_flag:
        context.output.write(
            "error: provide the hint either positionally or via --hint, not both",
            error=True,
        )
        return 2
    if hint_flag:
        hint = hint_flag
    list_hints = getattr(args, "list_hints", False)
    remove_hint = getattr(args, "remove_hint", None)

    if list_hints or (not hint and remove_hint is None):
        # List hints
        return _list_hints(context, issue_id, hints_file)
    elif remove_hint is not None:
        return _remove_hint(context, issue_id, hints_file, remove_hint)
    elif hint:
        no_wait = getattr(args, "no_wait", False)
        sock_path = _resolve_sock_path(context, issue_id)
        if sock_path is not None and not no_wait:

            async def _do_inject() -> int:
                t0 = asyncio.get_event_loop().time()

                # 1. Pause the agent so the message can be safely
                #    written to the transcript at a clean boundary.
                try:
                    pause_data = await _send_and_wait(
                        context,
                        sock_path,
                        "pause",
                        "",
                        "Paused",
                        timeout=30.0,
                    )
                    if pause_data is None:
                        context.output.write(
                            "warning: pause not confirmed within 30s — "
                            "agent may be in a long operation or already "
                            "paused. Injecting anyway.",
                            error=True,
                        )
                except (ConnectionRefusedError, FileNotFoundError, OSError):
                    # Socket gone — fall back to file.
                    return _inject_hint(context, issue_id, hints_file, hint)

                # 2. Inject the message (writes UserMessage to transcript
                #    + queues for in-memory Conversation).
                try:
                    data = await _send_and_wait(
                        context,
                        sock_path,
                        "inject",
                        hint,
                        "InjectDelivered",
                        timeout=30.0,
                    )
                except (ConnectionRefusedError, FileNotFoundError, OSError):
                    return _inject_hint(context, issue_id, hints_file, hint)

                # 3. Auto-resume so the agent processes the message.
                try:
                    await _send_and_wait(
                        context,
                        sock_path,
                        "resume",
                        "",
                        "Resumed",
                        timeout=30.0,
                    )
                except (ConnectionRefusedError, FileNotFoundError, OSError):
                    pass  # Best-effort resume

                elapsed = asyncio.get_event_loop().time() - t0
                if data is not None:
                    snippet = data.get("hint_snippet", "")
                    context.output.write(
                        f"Message injected and agent resumed ({elapsed:.1f}s). "
                        f"Agent will see it in its next response."
                    )
                    if snippet:
                        context.output.write(
                            f"  hint: {snippet}{'...' if len(hint) > 80 else ''}"
                        )
                    return 0
                else:
                    context.output.write(
                        f"Hint queued ({elapsed:.1f}s). "
                        f"Will be delivered at next tool result boundary."
                    )
                    return 0

            return await _do_inject()
        elif sock_path is not None and no_wait:
            if await _try_socket_inject(context, issue_id, hint):
                context.output.write(
                    f"\u2713 hint injected for issue {issue_id}"
                    f" \u00b7 agent will receive it at the next tool result boundary"
                )
                return 0
            return _inject_hint(context, issue_id, hints_file, hint)
        else:
            return _inject_hint(context, issue_id, hints_file, hint)
    else:
        return _list_hints(context, issue_id, hints_file)


def _parse_hints_file(
    context: CommandContext, hints_file: Path
) -> list[tuple[float, str]]:
    """Parse hints file into list of (timestamp, hint) tuples."""

    if not hints_file.exists():
        return []

    hints: list[tuple[float, str]] = []
    content = hints_file.read_text(encoding="utf-8")
    lines = content.split("\n")

    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if line.startswith("--- Operator Hint #"):
            ts_str = ""
            try:
                parts = line.split("(injected at ")
                if len(parts) > 1:
                    ts_str = parts[1].removesuffix(") ---")
                    from datetime import datetime

                    dt = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S")  # noqa: DTZ007 - legacy local timestamps
                    ts = dt.timestamp()
                else:
                    ts = time.time()
            except Exception:
                logger.debug("Command operation could not complete", exc_info=True)
                ts = time.time()

            hint_lines: list[str] = []
            i += 1
            while i < len(lines):
                if lines[i].strip().startswith("-" * 45):
                    break
                hint_lines.append(lines[i])
                i += 1
            hint = "\n".join(hint_lines).strip()
            if hint:
                hints.append((ts, hint))
        i += 1
    return hints


def _inject_hint(
    context: CommandContext, issue_id: str, hints_file: Path, hint: str
) -> int:
    """Append a hint to the .operator_hints.md file.

    Idempotent: if the hint text already exists in the file, it is
    not duplicated.
    """

    hints = _parse_hints_file(context, hints_file)
    # Idempotency — skip if the exact hint text
    # already exists.
    for _ts, existing_hint in hints:
        if existing_hint.strip() == hint.strip():
            context.output.write(
                f"Hint already exists for issue {issue_id} — no action taken."
            )
            return 0
    next_num = len(hints) + 1
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    header = f"--- Operator Hint #{next_num} (injected at {timestamp}) ---\n"
    separator = "-" * 50 + "\n"
    try:
        with open(hints_file, "a", encoding="utf-8") as f:
            f.write(header)
            f.write(hint + "\n")
            f.write(separator)
        context.output.write(
            f"\u2713 hint injected for issue {issue_id}"
            f" \u00b7 agent will pick it up at the next tool result boundary"
        )
        return 0
    except Exception as exc:
        logger.debug("Command operation could not complete", exc_info=True)
        context.output.write(f"Failed to inject hint: {exc}", error=True)
        return 1


def _list_hints(context: CommandContext, issue_id: str, hints_file: Path) -> int:
    """List all hints for an issue."""
    hints = _parse_hints_file(context, hints_file)
    if not hints:
        context.output.write(f"No hints for issue {issue_id}.")
        return 0
    context.output.write(f"Hints for issue {issue_id}:")
    for i, (ts, hint) in enumerate(hints, 1):
        ts_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))
        preview = hint[:60].replace("\n", " ")
        context.output.write(f"  #{i}: [{ts_str}] {preview}")
    return 0


def _remove_hint(
    context: CommandContext, issue_id: str, hints_file: Path, hint_num: int
) -> int:
    """Remove a hint by number."""
    hints = _parse_hints_file(context, hints_file)
    if hint_num < 1 or hint_num > len(hints):
        context.output.write(
            f"Hint #{hint_num} not found (have {len(hints)} hints).", error=True
        )
        return 1

    hints.pop(hint_num - 1)
    # Rebuild file

    content = ""
    for i, (ts, hint) in enumerate(hints, 1):
        ts_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))
        header = f"--- Operator Hint #{i} (injected at {ts_str}) ---\n"
        separator = "-" * 50 + "\n"
        content += header + hint + "\n" + separator
    hints_file.write_text(content, encoding="utf-8")
    context.output.write(f"Removed hint #{hint_num} for issue {issue_id}.")
    return 0


def _run_workspace(context: CommandContext, args: argparse.Namespace) -> int:
    """View or modify workspace files. Workspace listing/view are pure reads."""
    issue_id = getattr(args, "id", None)
    if not issue_id:
        context.output.write("error: --id is required", error=True)
        return 2

    ws_path = _resolve_issue_workspace_path(context, issue_id)
    if ws_path is None:
        context.output.write(
            f"Could not find workspace for issue {issue_id}.", error=True
        )
        return 1

    ls_flag = getattr(args, "ls", False)
    cat_flag = getattr(args, "cat", None)
    edit_flag = getattr(args, "edit", None)
    content = getattr(args, "content", None)

    if ls_flag:
        return _workspace_list_files(context, issue_id, ws_path)
    elif cat_flag:
        return _workspace_cat_file(context, issue_id, ws_path, cat_flag)
    elif edit_flag:
        if not content:
            context.output.write("error: --edit requires --with <content>", error=True)
            return 2
        return _workspace_edit_file(context, issue_id, ws_path, edit_flag, content)
    else:
        return _workspace_list_files(context, issue_id, ws_path)


def _workspace_list_files(context: CommandContext, issue_id: str, ws_path: Path) -> int:
    """List files in workspace. Idempotent — pure read."""
    if not ws_path.exists():
        context.output.write(f"Workspace for issue {issue_id} not found.", error=True)
        return 1

    exclude = {".metadata", ".orchestrator_control", ".operator_hints.md"}
    context.output.write(f"Workspace for issue {issue_id}: {ws_path}")
    context.output.write("-" * 60)

    files: list[str] = []
    dirs: list[str] = []
    for item in sorted(ws_path.iterdir()):
        if item.name in exclude:
            continue
        if item.is_dir():
            dirs.append(item.name + "/")
        else:
            size = item.stat().st_size
            files.append(f"{item.name} ({size} bytes)")

    for d in dirs:
        context.output.write(f"  [DIR]  {d}")
    for f in files:
        context.output.write(f"  {f}")
    if not files and not dirs:
        context.output.write("  (empty workspace)")
    return 0


def _workspace_cat_file(
    context: CommandContext, issue_id: str, ws_path: Path, filename: str
) -> int:
    """Show file contents. Idempotent — pure read."""
    file_path = ws_path / filename
    if not file_path.exists():
        context.output.write(f"File not found: {filename}", error=True)
        return 1
    if not file_path.is_file():
        context.output.write(f"Not a file: {filename}", error=True)
        return 1
    try:
        with file_path.open(encoding="utf-8") as stream:
            content = stream.read(
                -1 if context.output.limit is None else context.output.limit + 1
            )
        context.output.write(f"=== {filename} ===")
        context.output.write(content)
    except Exception as exc:
        logger.debug("Command operation could not complete", exc_info=True)
        context.output.write(f"Failed to read {filename}: {exc}", error=True)
        return 1
    return 0


def _workspace_edit_file(
    context: CommandContext, issue_id: str, ws_path: Path, filename: str, content: str
) -> int:
    """Write new content to a file."""
    file_path = ws_path / filename
    file_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        file_path.write_text(content, encoding="utf-8")
        context.output.write(f"Updated {filename} in issue {issue_id} workspace.")
        context.output.write("  The agent will see this change on its next tool call.")
        return 0
    except Exception as exc:
        logger.debug("Command operation could not complete", exc_info=True)
        context.output.write(f"Failed to write {filename}: {exc}", error=True)
        return 1


def _tracker_from_workflow_arg(
    context: CommandContext, args: argparse.Namespace
) -> Any | None:
    if context.runtime is not None:
        return context.runtime.tracker
    workflow_path = getattr(args, "workflow", None)
    if not workflow_path:
        return None
    try:
        from orchestratord.tracker import create_tracker_adapter
        from orchestratord.workflow import WorkflowLoader

        workflow, _ = WorkflowLoader.load(workflow_path)
        return create_tracker_adapter(workflow.tracker)
    except Exception as exc:
        logger.debug("Command operation could not complete", exc_info=True)
        context.output.write(
            f"Warning: could not initialize tracker from workflow: {exc}", error=True
        )
        return None


async def _mirror_intent_label(
    context: CommandContext,
    tracker: Any | None,
    issue_id: str,
    label: str,
    *,
    remove: bool,
) -> bool:
    """Best-effort mirror of CLI intent onto issue label.

    Calls ``tracker.add_label(issue_id, label)`` (default) or
    ``tracker.remove_label(issue_id, label)`` (when ``remove=True``)
    so the label-based intent path picks up the same intent as the
    local ``registry.intent``.

    The local ``registry.intent`` is the authoritative source of
    truth; this is belt-and-suspenders so a future registry reset
    does not silently drop the operator's intent. The function
    is intentionally permissive:

      * ``tracker is None`` → returns False (no-op).
      * Tracker does not implement the label method → returns False.
      * Async call raises or returns False → logs a warning and
        returns False. Never raises.

    Used by :func:`_run_retry` for ``--mode reset`` (add
    ``agent:retry``), ``--mode followup`` (add ``agent:follow-up``),
    and ``--mode unblock`` (remove ``agent:blocked``).
    """
    if tracker is None:
        return False
    from orchestratord.tracker import LabelCapability, supports

    if not supports(tracker, LabelCapability):
        return False
    method = tracker.remove_label if remove else tracker.add_label
    if method is None:
        return False
    try:

        async def call() -> bool:
            return bool(await method(issue_id, label))

        return await call()
    except Exception as exc:
        logger.debug("Command operation could not complete", exc_info=True)
        verb = "remove" if remove else "add"
        context.output.write(
            f"Warning: could not {verb} {label} label on issue {issue_id}: {exc}",
            error=True,
        )
        return False


async def _run_review(
    context: CommandContext,
    registry_path: Path | None,
    args: argparse.Namespace,
    workspace_root: str | Path | None = None,
) -> int:
    """Approve or reject a LocalTracker issue's changes."""
    issue_id = getattr(args, "id", None)
    if not issue_id:
        context.output.write("error: --id is required", error=True)
        return 2

    if not registry_path or not registry_path.exists():
        context.output.write(
            f"No registry found. Cannot review issue {issue_id}.", error=True
        )
        return 1

    from orchestratord.issue_registry import IssueStatus

    registry = context.registry(registry_path)
    record = registry.get(issue_id)
    if record is None:
        context.output.write(f"Issue {issue_id} not found in registry.", error=True)
        return 1

    approve = getattr(args, "approve", False)
    reject = getattr(args, "reject", False)

    if not approve and not reject:
        context.output.write("error: specify --approve or --reject", error=True)
        return 2

    end_reason = str(record.session_end_reason or "")
    recoverable_failed_completion = bool(
        record.status is IssueStatus.COMPLETED
        and (
            record.verification_status == "failed"
            or record.last_hook_error
            or end_reason == "empty_branch_no_commits"
            # Salvaged completions clear their verification fields on the
            # COMPLETED transition, so the salvage is only detectable via
            # the persisted end reason.
            or end_reason.startswith("salvaged_after_")
        )
    )
    retry_already_queued = bool(
        reject
        and (
            record.status
            in {
                IssueStatus.PENDING,
                IssueStatus.FAILED,
                IssueStatus.VERIFICATION_FAILED,
            }
            or recoverable_failed_completion
        )
        and (
            getattr(record.intent, "value", record.intent) in {"retry", "followup"}
            or record.commit_sha
            or record.pr_number
            or record.pr_url
            or recoverable_failed_completion
        )
    )
    approve_already_recorded = approve and record.status is IssueStatus.COMPLETED
    if (
        record.status is not IssueStatus.PENDING_REVIEW
        and not retry_already_queued
        and not approve_already_recorded
    ):
        context.output.write(
            f"Issue {issue_id} is not pending review (status: {record.status.value}).",
            error=True,
        )
        context.output.write(
            "Only issues with 'pending_review' status, or an already queued "
            "rejection retry, can be reviewed.",
            error=True,
        )
        return 1

    if reject:
        feedback = getattr(args, "feedback", None)
        if not feedback:
            context.output.write("error: --reject requires --feedback", error=True)
            return 2

        # The daemon owns its in-memory registry, lifecycle sets, tracker state,
        # and clarification queue. Send one durable control command so those
        # related mutations happen together on the next poll instead of
        # partially updating the same files through stale CLI-side objects.
        rc = await _write_control(
            context, "review_retry", issue_id, feedback, workspace_root=workspace_root
        )
        if rc != 0:
            return rc

        context.output.write(f"Issue {issue_id} rejected with feedback:")
        context.output.write(f'  "{feedback}"')
        context.output.write("Feedback queued — orchestrator will retry this issue.")
        return 0

    if approve:
        comment = getattr(args, "comment", None)
        rc = await _write_control(
            context,
            "review_approve",
            issue_id,
            comment or "",
            workspace_root=workspace_root,
        )
        if rc != 0:
            return rc

        # The daemon is the sole owner of the registry, lifecycle sets and
        # remote tracker side effects.  Updating them here as well races its
        # in-memory snapshot and posts the optional approval comment twice.
        context.output.write(
            f"Issue {issue_id} approval queued — orchestrator will finalize it."
        )
        return 0


def _fallback_feedback_url(
    context: CommandContext, record: Any, feedback_id: str
) -> str | None:
    """Reconstruct a comment URL when none was persisted.

    Used by ``issue feedback --list`` for records written before URL
    persistence, or items whose source has no html_url (GitCode's
    issue-comments endpoint omits it). Parses host/owner/repo from the
    record's ``pr_url`` and builds the platform's comment permalink:

      - gitcode / gitee: ``{host}/{owner}/{repo}/issues/{number}#tid-{id}``
      - github:          ``{host}/{owner}/{repo}/issues/{number}#issuecomment-{id}``

    Returns ``None`` for review_summary / ci sources (no comment anchor)
    or when the record has no parseable pr_url.
    """
    if not feedback_id or ":" not in feedback_id:
        return None
    source, _, raw_id = feedback_id.partition(":")
    if source not in {"conversation", "inline_review"} or not raw_id:
        return None
    pr_url = getattr(record, "pr_url", None)
    if not isinstance(pr_url, str) or not pr_url:
        return None
    m = _PR_URL_RE.match(pr_url)
    if not m:
        return None
    host = m.group("host")
    owner = m.group("owner")
    repo = m.group("repo")
    # Issue/PR number for the URL path. The tracker fetches conversation
    # comments via ``/issues/{effective_issue_id}/comments`` where
    # ``effective_issue_id = issue_id or pr_number`` (see
    # client.fetch_pull_request_feedback), so the comment lives under the
    # issue number. Prefer the record's issue_id when it is numeric
    # (GitCode stores the bare issue number there); fall back to pr_number
    # for GitHub/Gitee where issue_id may be a tracker key (AGENTSDK-15).
    number = ""
    raw_issue_id = str(getattr(record, "issue_id", "") or "").strip()
    raw_issue_id = raw_issue_id.removeprefix("#")
    if raw_issue_id.isdigit():
        number = raw_issue_id
    else:
        pr_number = getattr(record, "pr_number", None)
        if isinstance(pr_number, str) and pr_number.strip().isdigit():
            number = pr_number.strip()
    if not number:
        return None
    anchor = f"#issuecomment-{raw_id}" if "github.com" in host else f"#tid-{raw_id}"
    return f"{host}/{owner}/{repo}/issues/{number}{anchor}"


async def _run_feedback(
    context: CommandContext,
    registry_path: Path | None,
    args: argparse.Namespace,
    workspace_root: str | Path | None = None,
) -> int:
    """List, approve, or dismiss pending PR review feedback."""
    issue_id = getattr(args, "id", None)
    if not issue_id:
        context.output.write("error: --id is required", error=True)
        return 2

    if not registry_path or not registry_path.exists():
        context.output.write(
            f"No registry found. Cannot manage feedback for issue {issue_id}.",
            error=True,
        )
        return 1

    registry = context.registry(registry_path)
    record = registry.get(issue_id)
    if record is None:
        context.output.write(f"Issue {issue_id} not found in registry.", error=True)
        return 1

    list_feedback = getattr(args, "list_feedback", False)
    approve = getattr(args, "approve", False)
    dismiss = getattr(args, "dismiss", False)

    if not list_feedback and not approve and not dismiss:
        context.output.write(
            "error: specify --list, --approve, or --dismiss", error=True
        )
        return 2

    if list_feedback:
        if not record.pending_feedback_ids:
            context.output.write(f"No pending feedback for issue {issue_id}.")
            return 0
        context.output.write(f"Pending feedback for issue {issue_id}:")
        context.output.write(
            "(use the ID with --feedback-id to approve/dismiss a single item)"
        )
        for i, fid in enumerate(record.pending_feedback_ids, 1):
            # Resolve the canonical comment/check URL when available
            # (persisted from the tracker's html_url). Fall back to
            # reconstructing it from pr_url + raw comment id (GitCode's
            # issue-comments API omits html_url). No URL for review_summary
            # / ci sources -> show the id alone.
            url = record.pending_feedback_urls.get(fid) or _fallback_feedback_url(
                context, record, fid
            )
            if url:
                context.output.write(f"  {i}. {fid}  ->  {url}")
            else:
                context.output.write(f"  {i}. {fid}")
        context.output.write(
            f"\nTotal: {len(record.pending_feedback_ids)} pending item(s)"
        )
        return 0

    target_ids = getattr(args, "feedback_id", None) or list(record.pending_feedback_ids)
    if not target_ids:
        context.output.write(f"No pending feedback to process for issue {issue_id}.")
        return 0

    if dismiss:
        registry.mark_feedback_processed(issue_id, target_ids)
        context.output.write(
            f"Dismissed {len(target_ids)} feedback item(s) for issue {issue_id}."
        )
        return 0

    if approve:
        rc = await _write_control(
            context,
            "review_followup",
            issue_id,
            ",".join(target_ids),
            workspace_root=workspace_root,
        )
        if rc != 0:
            return rc
        context.output.write(
            f"Approved {len(target_ids)} feedback item(s) for issue {issue_id}."
        )
        context.output.write(
            "Follow-up will be triggered on next orchestrator poll cycle."
        )
        return 0

    return 0


def _get_status_str(context: CommandContext, status) -> str:
    """Normalize status to string."""
    if hasattr(status, "value"):
        return status.value
    return str(status)


def _resolve_operator(context: CommandContext, explicit: str | None) -> str:
    """Resolve the operator login for audit logging.

    Priority: explicit --operator arg > $USER env > os.getlogin() > 'unknown'.
    """
    if explicit:
        return explicit
    env_user = os.environ.get("USER") or os.environ.get("USERNAME")
    if env_user:
        return env_user
    try:
        return os.getlogin()
    except Exception:
        logger.debug("Command operation could not complete", exc_info=True)
        return "unknown"


def _append_audit_log(
    context: CommandContext,
    *,
    issue_id: str,
    mode: str,
    reason: str,
    operator: str,
    force: bool,
    extra: dict[str, Any] | None = None,
    path: Path | None = None,
) -> Path | None:
    """Append a single JSONL line to the local audit log.

    Design: "~/.orchestratord/orchestrator/audit.jsonl 记录
    {ts, operator, issue_id, mode, reason} 便于追溯".

    Returns the path written, or None on I/O failure (the CLI surfaces
    audit failures to the operator as a warning but does not abort —
    the registry update is the user-visible side-effect).
    """
    import json

    target = path or context.audit_path
    payload: dict[str, Any] = {
        "ts": time.time(),
        "ts_iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "operator": operator,
        "issue_id": issue_id,
        "mode": mode,
        "reason": reason,
        "force": force,
        "priority": "high" if force else "normal",
    }
    if extra:
        payload.update(extra)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
        return target
    except Exception as exc:
        logger.debug("Command operation could not complete", exc_info=True)
        context.output.write(
            f"warning: failed to write audit log {target}: {exc}",
            error=True,
        )
        return None


async def _run_rebase(
    context: CommandContext,
    registry_path: Path | None,
    args: argparse.Namespace,
    workspace_root: str | Path | None = None,
) -> int:
    """CLI 兜底命令 — request a PR rebase via the built-in path.

    Unlike ``issue retry``, this command DOES NOT mutate the local
    registry intent directly. Instead it writes a control file that
    the daemon picks up on its next poll cycle and dispatches to
    ``_process_rebase_intent`` (which calls ``git_sync.rebase_for_pr``
    directly, with no external agent involvement when the rebase is
    clean). This avoids racing the daemon when it is mid-run.

    ``--force`` (default False) overrides two safe defaults:

      1. Uses plain ``git push --force`` instead of
         ``--force-with-lease``. May overwrite concurrent pushes.
      2. Bypasses the ``max_rebase_attempts_per_issue`` rate-limit
         gate. The audit entry is flagged high-priority in either
         case so the operator action is traceable.
    """
    issue_id = getattr(args, "id", None)
    if not issue_id:
        context.output.write("error: --id is required for rebase", error=True)
        return 2
    force = bool(getattr(args, "force", False))
    reason = getattr(args, "reason", "") or ""
    operator = _resolve_operator(context, getattr(args, "operator", None))

    if registry_path is None or not registry_path.exists():
        context.output.write(
            "error: no issue registry found for this workspace.\n"
            "hint: run from a project root or pass --workspace / --workflow.",
            error=True,
        )
        return 1

    registry = context.registry(registry_path)
    record = registry.get_by_issue_ref(issue_id)
    if record is None:
        # Auto-register so the daemon can find the record on its next
        # poll. CLI rebase is a legitimate way to bootstrap an issue
        # record when the local daemon hasn't seen the issue yet.
        registry.register(
            issue_id=issue_id,
            issue_identifier=issue_id,
        )
        record = registry.get(issue_id)
        assert record is not None
    registry_issue_id = record.issue_id

    # Guard 1: the issue must have a known PR + workspace + branch.
    # Without these the rebase cannot be performed (no PR to push to,
    # no local workspace to operate on).
    if not record.pr_number or not record.workspace_path or not record.branch_name:
        context.output.write(
            f"error: issue {issue_id} ({record.issue_identifier}) has "
            f"no PR / workspace / branch registered. The rebase path "
            f"requires a previously-opened PR. Run a normal agent "
            f"cycle first to open a PR, then re-issue this command.",
            error=True,
        )
        return 4

    # Mirror the intent onto the local registry so the daemon's
    # _resolve_intent sees REBASE even when the control file path is
    # unavailable (e.g. daemon already started its poll cycle).
    from orchestratord.tracker import Intent

    registry.mark_intent(
        registry_issue_id,
        Intent.REBASE,
        source="cli",
        command=f"cli:rebase:{reason[:64]}",
    )

    # Guard 2: best-effort rate-limit preview (the daemon enforces
    # the authoritative gate via _check_rebase_rate_limit). When the
    # current count already equals the configured cap and the
    # operator did NOT pass --force, warn but still write the control
    # file — the daemon's gate will surface a structured
    # ``rebase_rejected`` audit event instead of silently swallowing
    # the request.
    max_attempts = 3
    if record.rebase_attempt_count >= max_attempts and not force:
        context.output.write(
            f"warning: issue {issue_id} has reached "
            f"rebase_attempt_count={record.rebase_attempt_count} >= "
            f"max_rebase_attempts_per_issue={max_attempts}. Pass "
            f"--force to bypass (logged as high-priority audit).",
            error=True,
        )

    # Mirror the intent label onto the tracker (best-effort).
    tracker = _tracker_from_workflow_arg(context, args)
    if tracker is not None:
        await _mirror_intent_label(
            context, tracker, issue_id, "agent:rebase", remove=False
        )

    # Append a JSONL entry to the local audit log so the operator
    # action is traceable.
    _append_audit_log(
        context,
        issue_id=issue_id,
        mode="rebase",
        reason=reason,
        operator=operator,
        force=force,
        extra={
            "issue_identifier": record.issue_identifier,
            "event": "rebase_requested",
            "priority": "high" if force else "normal",
            "push_method": "force" if force else "force-with-lease",
            "rebase_attempt_count": record.rebase_attempt_count,
            "max_rebase_attempts_per_issue": max_attempts,
            "pr_number": record.pr_number,
            "branch_name": record.branch_name,
            "base_branch": record.base_branch,
        },
    )

    # Write the control file that the daemon polls. Format:
    #   rebase\n<id>\nforce=0|1\n<reason>\n
    extra = f"force={'1' if force else '0'}\n{reason}"
    rc = await _write_control(
        context, "rebase", registry_issue_id, extra, workspace_root=workspace_root
    )
    if rc != 0:
        return rc

    context.output.write(
        f"Issue {issue_id} ({record.issue_identifier}): rebase requested."
    )
    context.output.write(
        f"  push method: {'--force' if force else '--force-with-lease'}"
    )
    if reason:
        context.output.write(f"  reason: {reason}")
    context.output.write(
        "  The orchestrator will run `rebase_for_pr` on its next poll cycle (default 30s)."
    )
    return 0


async def _run_retry(
    context: CommandContext,
    registry_path: Path | None,
    args: argparse.Namespace,
    *,
    workspace_root: str | Path | None = None,
) -> int:
    """CLI 兜底命令 — record an operator-driven retry intent.

    Behaviour (per the design doc):

      * ``--mode reset``    — mark intent=RETRY, reset the registry record
                              to PENDING, and reopen the workflow tracker
                              issue so the daemon can pick it up.
      * ``--mode followup`` — mark intent=FOLLOWUP so Sub-C reuses the
                              existing branch.
      * ``--mode unblock``  — call IssueRegistry.unblock() to roll an
                              ABANDONED issue back to PENDING.

    All three branches append a JSONL entry to the local audit log
    (~/.orchestratord/orchestrator/audit.jsonl) so the action is
    traceable. ``--force`` flags the audit entry as high-priority
    and signals that the rate limit (Sub-F) was bypassed.
    """
    issue_id = getattr(args, "id", None)
    if not issue_id:
        context.output.write("error: --id is required for retry", error=True)
        return 2
    mode = getattr(args, "mode", None)
    if mode not in {"reset", "followup", "unblock"}:
        context.output.write(
            f"error: --mode must be reset|followup|unblock, got {mode!r}", error=True
        )
        return 2
    reason = getattr(args, "reason", "") or ""
    force = bool(getattr(args, "force", False))
    operator = _resolve_operator(context, getattr(args, "operator", None))
    max_retries = int(getattr(args, "max_retries", 3) or 3)

    if registry_path is None or not registry_path.exists():
        context.output.write(
            "error: no issue registry found for this workspace.\n"
            "hint: run from a project root or pass --workspace / --workflow.",
            error=True,
        )
        return 1

    from orchestratord.tracker import Intent

    registry = context.registry(registry_path)
    record = registry.get_by_issue_ref(issue_id)

    # --stop-first: if the agent is still running, stop it
    # before retrying. Equivalent to 'issue stop' + 'issue retry'.
    stop_first = bool(getattr(args, "stop_first", False))
    if stop_first and record is not None and record.status.value == "running":
        sock_path = _resolve_sock_path(context, issue_id, workspace_root)
        if sock_path is not None:
            context.output.write(f"Stopping running agent for {issue_id} before retry…")

            async def _stop_for_retry() -> bool:
                data = await _send_and_wait(
                    context, sock_path, "stop", "", "SessionComplete", timeout=10.0
                )
                return data is not None

            stopped = await _stop_for_retry()
            if stopped:
                context.output.write("Agent stopped. Proceeding with retry.")
            else:
                context.output.write(
                    "warning: stop sent but agent may still be unwinding. "
                    "Proceeding with retry anyway.",
                    error=True,
                )
        else:
            context.output.write(
                f"warning: could not find control socket for {issue_id}. "
                f"Writing stop control file as fallback.",
                error=True,
            )
            await _write_control(
                context, "stop", issue_id, workspace_root=workspace_root
            )
    elif stop_first and record is not None and record.status.value != "running":
        context.output.write(
            f"Issue {issue_id} is not running (status: {record.status.value}). "
            f"No need to stop before retry."
        )
    if record is None:
        # Auto-register so the daemon can find the record on its next
        # poll. CLI retry is a legitimate way to bootstrap an issue
        # record when the local daemon hasn't seen the issue yet.
        registry.register(
            issue_id=issue_id,
            issue_identifier=issue_id,
        )
        record = registry.get(issue_id)
        assert record is not None  # just registered
    registry_issue_id = record.issue_id

    # ``--mode reset`` is itself a fresh-start bypass: it clears
    # ``retry_count`` via ``reset_for_retry(reset_retry_count=True)``,
    # so the ``max_retries_per_issue`` cap does not apply (you cannot
    # be locked out of a command whose whole point is to wipe the
    # lock). ``--force`` is still accepted as an audit-priority
    # marker (the original design required high-priority entries
    # for cap bypasses) but no longer gates the cap check.
    #
    # Other retry paths (label-driven ``agent:retry``, comment-driven
    # ``/agent retry``) DO respect the cap — they live in
    # ``orchestrator._resolve_intent`` / ``mark_intent`` and call
    # ``reset_for_retry(increment_retry=True)`` to bump the budget
    # one tick at a time.
    rate_limited = False
    control_rc = 0

    if rate_limited:
        action = "rate-limited (--force required)"
        audit_priority = "high"
        audit_event = "retry_rejected"
    else:
        # Obtain the tracker once so we can
        # mirror the CLI intent onto the remote issue label AND
        # reopen the issue. The tracker is optional — operators
        # who run from a directory without a workflow.md will get
        # None and the local registry.intent (written just below)
        # is still the authoritative source of truth.
        tracker = _tracker_from_workflow_arg(context, args)
        if mode == "reset":
            registry.mark_intent(
                registry_issue_id,
                Intent.RETRY,
                source="cli",
                command=f"cli:reset:{reason[:64]}",
            )
            # ``mode=reset`` is semantically a fresh start: clear the
            # previous failure state AND reset the rate-limit budget so a
            # transient daemon/agent bug that consumed the previous
            # retries does not permanently lock the issue. Other retry
            # paths (label-driven ``agent:retry``, comment-driven
            # ``/agent retry``) keep the historical ``+= 1`` behaviour
            # via the default ``increment_retry=True``.
            registry.reset_for_retry(registry_issue_id, reset_retry_count=True)
            if tracker is not None:
                try:

                    async def reopen_tracker_issue() -> None:
                        try:
                            await tracker.update_issue_state(issue_id, "open")
                        except FileNotFoundError:
                            if registry_issue_id == issue_id:
                                raise
                            await tracker.update_issue_state(registry_issue_id, "open")

                    await reopen_tracker_issue()
                except Exception as exc:
                    logger.debug("Command operation could not complete", exc_info=True)
                    context.output.write(
                        f"Warning: could not update tracker: {exc}", error=True
                    )
                # Mirror the retry intent onto the remote issue
                # label so label-based intent resolution sees the
                # same intent. Best-effort: the tracker may not
                # implement add_label (returns False), or the API
                # call may fail — both are non-fatal because the
                # local registry.intent is the authoritative
                # source.
                await _mirror_intent_label(
                    context, tracker, issue_id, "agent:retry", remove=False
                )
            # Lifecycle bookkeeping also lives in the daemon's completed /
            # running sets. Notify its durable control queue even when this
            # service already shares the live registry in-process.
            control_root = workspace_root or registry_path.parent
            control_rc = await _write_control(
                context,
                "retry",
                registry_issue_id,
                reason,
                workspace_root=control_root,
            )
            action = (
                "marked for reset"
                if control_rc == 0
                else "reset persisted, but daemon notification failed"
            )
        elif mode == "followup":
            registry.mark_intent(
                registry_issue_id,
                Intent.FOLLOWUP,
                source="cli",
                command=f"cli:followup:{reason[:64]}",
            )
            if tracker is not None:
                await _mirror_intent_label(
                    context, tracker, issue_id, "agent:follow-up", remove=False
                )
            # Write the reason to .operator_hints.md so the agent
            # sees it as context on re-launch (mirrors the chat
            # followup path in dashboard.py).
            if reason:
                ws_path = (
                    Path(record.workspace_path)
                    if record.workspace_path
                    else (Path(workspace_root) if workspace_root else None)
                )
                if ws_path is not None:
                    _inject_hint(
                        context,
                        registry_issue_id,
                        ws_path / ".operator_hints.md",
                        reason,
                    )
            # Notify the daemon via control file so it picks up the
            # intent immediately instead of waiting for the next poll.
            control_root = workspace_root or registry_path.parent
            control_rc = await _write_control(
                context,
                "followup",
                registry_issue_id,
                reason,
                workspace_root=control_root,
            )
            action = (
                "marked for follow-up"
                if control_rc == 0
                else "followup intent persisted, but daemon notification failed"
            )
        else:  # mode == "unblock"
            registry.unblock(registry_issue_id)
            if tracker is not None:
                await _mirror_intent_label(
                    context, tracker, issue_id, "agent:blocked", remove=True
                )
            action = "unblocked"
        audit_priority = "high" if force else "normal"
        audit_event = "retry" if mode == "reset" else mode

    audit_path = _append_audit_log(
        context,
        issue_id=issue_id,
        mode=mode,
        reason=reason,
        operator=operator,
        force=force,
        extra={
            "issue_identifier": record.issue_identifier,
            "event": audit_event,
            "priority": audit_priority,
            "retry_count": record.retry_count,
            "max_retries_per_issue": max_retries,
            "rate_limited": rate_limited,
        },
    )

    context.output.write(f"Issue {issue_id} ({record.issue_identifier}): {action}.")
    if reason:
        context.output.write(f"  reason: {reason}")
    context.output.write(f"  operator: {operator}")
    if rate_limited:
        context.output.write(
            f"  rate limit: retry_count={record.retry_count} >= "
            f"max_retries_per_issue={max_retries}.\n"
            f"  Re-run with --force to bypass (logged as high-priority audit).",
            error=True,
        )
    if force and not rate_limited:
        context.output.write(
            "  (--force set: rate limit bypassed, audit entry marked high-priority)"
        )
    if audit_path is not None:
        context.output.write(f"  audit log: {audit_path}")
    if control_rc == 0:
        context.output.write(
            "  The orchestrator will pick this up on its next poll cycle."
        )
    else:
        context.output.write(
            "  The local reset was saved, but the orchestrator control command failed.",
            error=True,
        )
    if rate_limited:
        return 3
    return control_rc
