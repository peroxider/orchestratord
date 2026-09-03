"""orchestrator server — manage the orchestrator daemon process.

Usage (noun-verb):
  orchestratord server status                                   Show orchestrator daemon status
  orchestratord server stop                                     Stop the orchestrator daemon gracefully
  orchestratord server start [--workflow PATH]                  Start the orchestrator daemon
  orchestratord server start [--workflow PATH]                  Start with declarative workflow engine
                                       [--workflow-yaml PATH]

All commands are idempotent:
  - status: pure read, always safe
  - stop: stopping an already-stopped daemon succeeds silently
  - start: starting an already-running daemon shows its status and exits 0
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import signal
import sys
import time
import uuid
from pathlib import Path
from typing import Any

from orchestratord.paths import GATEWAY_SOCK
from orchestratord.workspace_locator import ORCHESTRATORD_ORCHESTRATOR_DIR

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Parser registration
# ---------------------------------------------------------------------------


def add_server_parser(
    subparsers: argparse._SubParsersAction,
    *,
    command_name: str = "server",
    dest: str = "server_subcommand",
    start_command: str = "start",
) -> None:
    """Register daemon commands under the canonical or compatibility name."""
    server_parser = subparsers.add_parser(
        command_name,
        help="Manage the orchestrator daemon process",
        description="Start, stop, or check the status of the orchestrator daemon. "
        "All commands are idempotent — running them multiple times "
        "has no ill effect.",
    )
    server_sub = server_parser.add_subparsers(
        dest=dest,
        required=True,
    )

    # --- server status ---
    status_parser = server_sub.add_parser(
        "status",
        help="Show orchestrator daemon status",
        description="Display whether the orchestrator daemon is running, its PID, "
        "uptime, workspace root, and project slug. Idempotent (pure read).",
    )
    status_parser.add_argument(
        "--workspace",
        type=str,
        default=None,
        metavar="PATH",
        help="Explicit workspace root path (optional auto-detection override)",
    )
    status_parser.add_argument(
        "--workflow",
        type=str,
        default=None,
        metavar="PATH",
        help="Path to WORKFLOW.md (helps resolve workspace when metadata is missing)",
    )

    # --- server stop ---
    stop_parser = server_sub.add_parser(
        "stop",
        help="Stop the orchestrator daemon gracefully",
        description="Send SIGTERM to the orchestrator process and clean up metadata. "
        "Idempotent: if the daemon is already stopped, exits 0 silently.",
    )
    stop_parser.add_argument(
        "--workspace",
        type=str,
        default=None,
        metavar="PATH",
        help="Explicit workspace root path (optional auto-detection override)",
    )
    stop_parser.add_argument(
        "--workflow",
        type=str,
        default=None,
        metavar="PATH",
        help="Path to WORKFLOW.md (helps resolve workspace when metadata is missing)",
    )
    stop_parser.add_argument(
        "--force",
        action="store_true",
        help="Use SIGKILL instead of SIGTERM (force immediate termination)",
    )
    stop_parser.add_argument(
        "--timeout",
        type=float,
        default=5.0,
        metavar="SECONDS",
        help="Seconds to wait after SIGTERM before SIGKILL (default: 5.0)",
    )
    stop_parser.add_argument(
        "--all",
        action="store_true",
        help="Stop all running orchestrator daemons and clean up all stale metadata. "
        "Useful after test suites or when multiple workflows were started.",
    )

    # --- server start ---
    start_parser = server_sub.add_parser(
        start_command,
        help="Start the orchestrator daemon",
        description="Launch the orchestrator with a workflow file. "
        "Optionally enable the declarative workflow engine via --workflow-yaml "
        "for multi-stage DAG execution with quality gates and decision branches.",
        epilog="Examples:\n"
        "  orchestratord server start --workflow ./workflow.md\n"
        "  orchestratord server start --workflow ./workflow.md --workflow-yaml ./workflow.yaml\n"
        "  orchestratord server start --workflow ./workflow.md --workflow-yaml ./workflow.yaml --dashboard",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    start_parser.add_argument(
        "--workflow",
        type=str,
        required=False,
        metavar="PATH",
        help="Path to WORKFLOW.md file",
    )
    start_parser.add_argument(
        "--workflow-yaml",
        type=str,
        default=None,
        metavar="PATH",
        help="Path to workflow.yaml for the declarative workflow engine",
    )
    start_parser.add_argument(
        "--dashboard",
        action="store_true",
        help="Show embedded status dashboard",
    )
    start_parser.add_argument(
        "--port",
        type=int,
        default=None,
        help="LiveView dashboard port",
    )
    start_parser.add_argument(
        "--gateway",
        dest="gateway",
        action="store_true",
        help="Opt into all supported direct/private messages via the IM gateway",
    )
    start_parser.add_argument(
        "--im-gateway",
        dest="gateway",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    start_parser.add_argument(
        "--gateway-origin",
        dest="gateway_origin",
        type=str,
        default=None,
        metavar="ORIGIN",
        help=(
            "Advanced: opt into the IM gateway for a specific origin, e.g. "
            "wechat:direct:default:user_id"
        ),
    )
    start_parser.add_argument(
        "--im-gateway-origin",
        dest="gateway_origin",
        type=str,
        default=None,
        metavar="ORIGIN",
        help=argparse.SUPPRESS,
    )
    start_parser.add_argument(
        "--gateway-sock",
        dest="gateway_sock",
        type=str,
        default=None,
        metavar="PATH",
        help=(
            "Gateway daemon Unix socket for --gateway-origin "
            "(default: ~/.orchestratord/gateway/gateway.sock)"
        ),
    )
    start_parser.add_argument(
        "--im-gateway-sock",
        dest="gateway_sock",
        type=str,
        default=None,
        metavar="PATH",
        help=argparse.SUPPRESS,
    )
    start_parser.add_argument(
        "--backend",
        type=str,
        required=True,
        metavar="NAME",
        help="Backend identifier registered through the orchestratord backend SPI.",
    )

    # --- server connect-gateway ---
    connect_parser = server_sub.add_parser(
        "connect-gateway",
        help="Ask a running daemon to connect to the IM gateway",
        description=(
            "Submit an IM gateway connect request to an already-running orchestrator daemon. "
            "The daemon handles the request on its next control-file poll."
        ),
    )
    connect_parser.add_argument(
        "--workspace",
        type=str,
        default=None,
        metavar="PATH",
        help="Explicit workspace root path (optional auto-detection override)",
    )
    connect_parser.add_argument(
        "--workflow",
        type=str,
        default=None,
        metavar="PATH",
        help="Path to WORKFLOW.md (helps resolve workspace when metadata is missing)",
    )
    connect_parser.add_argument(
        "--gateway",
        dest="gateway",
        type=str,
        default=None,
        metavar="ORIGIN",
        help=(
            "Optional specific origin to bind, e.g. wechat:direct:default:user_id. "
            "Omit for all supported direct/private IM messages."
        ),
    )
    connect_parser.add_argument(
        "--im-gateway",
        dest="gateway",
        type=str,
        default=None,
        metavar="ORIGIN",
        help=argparse.SUPPRESS,
    )
    connect_parser.add_argument(
        "--gateway-sock",
        dest="gateway_sock",
        type=str,
        default=None,
        metavar="PATH",
        help=(
            "Gateway daemon Unix socket for connect-gateway "
            "(default: ~/.orchestratord/gateway/gateway.sock)"
        ),
    )
    connect_parser.add_argument(
        "--im-gateway-sock",
        dest="gateway_sock",
        type=str,
        default=None,
        metavar="PATH",
        help=argparse.SUPPRESS,
    )

    # --- server disconnect-gateway ---
    disconnect_parser = server_sub.add_parser(
        "disconnect-gateway",
        help="Ask a running daemon to disconnect from the IM gateway",
        description=(
            "Submit an IM gateway disconnect request to an already-running orchestrator daemon. "
            "The daemon handles the request on its next control-file poll."
        ),
    )
    disconnect_parser.add_argument(
        "--workspace",
        type=str,
        default=None,
        metavar="PATH",
        help="Explicit workspace root path (optional auto-detection override)",
    )
    disconnect_parser.add_argument(
        "--workflow",
        type=str,
        default=None,
        metavar="PATH",
        help="Path to WORKFLOW.md (helps resolve workspace when metadata is missing)",
    )


# ---------------------------------------------------------------------------
# Run dispatch
# ---------------------------------------------------------------------------


def run(args: argparse.Namespace) -> int:
    """Dispatch to the appropriate server subcommand."""
    cmd = getattr(args, "server_subcommand", None) or getattr(args, "daemon_subcommand", None)
    if cmd == "status":
        return _run_status(args)
    elif cmd == "stop":
        return _run_stop(args)
    elif cmd in ("start", "serve"):
        return _run_start(args)
    elif cmd == "connect-gateway":
        return _run_connect_gateway(args)
    elif cmd == "disconnect-gateway":
        return _run_disconnect_gateway(args)
    print(f"error: unknown server subcommand '{cmd}'", file=sys.stderr)
    return 2


# ---------------------------------------------------------------------------
# Implementation
# ---------------------------------------------------------------------------


def _find_metadata(args: argparse.Namespace) -> tuple[Path | None, dict | None]:
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
            print_multi_project_hint,
        )

        live = get_live_projects()
        if len(live) > 1:
            subcmd = getattr(args, "server_subcommand", "server")
            print_multi_project_hint(live, f"orchestrator server {subcmd}")
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

        slug = _slug_from_workspace(workspace_root_str)
        metadata_path = ORCHESTRATORD_ORCHESTRATOR_DIR / slug / "metadata.json"
        if metadata_path.exists():
            import json

            try:
                data = json.loads(metadata_path.read_text(encoding="utf-8"))
                return metadata_path, data
            except Exception:
                pass
        # Fallback: search by workspace_root matching
        if ORCHESTRATORD_ORCHESTRATOR_DIR.exists():
            for md_dir in ORCHESTRATORD_ORCHESTRATOR_DIR.iterdir():
                mf = md_dir / "metadata.json"
                if mf.exists():
                    import json

                    try:
                        data = json.loads(mf.read_text(encoding="utf-8"))
                        if _same_root(data.get("workspace_root")):
                            return mf, data
                    except Exception:
                        pass

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
                pass

    return None, None


def _slug_from_workspace(ws_str: str) -> str:
    """Generate a deterministic slug from a workspace path string."""
    parts = [
        p
        for p in ws_str.strip().replace("/", "-").replace("\\", "-").split("-")
        if p and p not in ("tmp", ".orchestratord", "~")
    ]
    return "-".join(parts[-3:]) if parts else "default"


def _is_pid_alive(pid: int) -> bool:
    """Check whether a PID is still alive (no-side-effect signal 0 test)."""
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def _format_uptime(started_at: float) -> str:
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


# ---------------------------------------------------------------------------
# server status
# ---------------------------------------------------------------------------


def _run_status(args: argparse.Namespace) -> int:
    """Show orchestrator daemon status. Idempotent — pure read."""
    meta_path, meta = _find_metadata(args)

    if meta is None:
        print("Orchestrator daemon: NOT RUNNING")
        print("  No orchestrator metadata found.")
        print("  Hint: Start with 'orchestratord server start --workflow WORKFLOW.md'")
        return 0  # idempotent: not-running is a valid status, not an error

    pid = meta.get("pid")
    started_at = meta.get("started_at", 0)
    project_slug = meta.get("project_slug", "unknown")
    workspace_root = meta.get("workspace_root", "unknown")
    workflow_path = meta.get("workflow_path")

    if pid and _is_pid_alive(pid):
        uptime = _format_uptime(started_at) if started_at else "unknown"
        print("Orchestrator daemon: RUNNING")
        print(f"  PID            : {pid}")
        print(f"  Uptime         : {uptime}")
        print(f"  Project        : {project_slug}")
        print(f"  Workspace root : {workspace_root}")
        if workflow_path:
            print(f"  Workflow       : {workflow_path}")
        print(f"  Metadata       : {meta_path}")
    else:
        stale_age = _format_uptime(started_at) if started_at else "unknown"
        print(f"Orchestrator daemon: STOPPED (stale metadata from {stale_age} ago)")
        print(f"  Project        : {project_slug}")
        print(f"  Workspace root : {workspace_root}")
        print(f"  Metadata       : {meta_path} (stale — clean up with 'server stop')")
        # Auto-clean stale metadata
        if meta_path and meta_path.exists():
            meta_path.unlink()
            print("  -> Stale metadata cleaned up.")

    return 0


# ---------------------------------------------------------------------------
# server stop
# ---------------------------------------------------------------------------


def _run_stop_all(args: argparse.Namespace) -> int:
    """Stop all running orchestrator daemons and clean up all stale metadata.

    Iterates every metadata file under ``~/.orchestratord/orchestrator/*/metadata.json``.
    - Live PIDs → send signal (SIGTERM / SIGKILL) and wait for graceful exit.
    - Dead PIDs → clean up stale metadata immediately.
    """
    orchestrator_dir = ORCHESTRATORD_ORCHESTRATOR_DIR
    if not orchestrator_dir.exists():
        print("No orchestrator metadata directory found — nothing to stop.")
        return 0

    metadata_files: list[tuple[Path, dict]] = []
    for md_dir in orchestrator_dir.iterdir():
        if not md_dir.is_dir():
            continue
        mf = md_dir / "metadata.json"
        if not mf.exists():
            continue
        try:
            data = json.loads(mf.read_text(encoding="utf-8"))
            metadata_files.append((mf, data))
        except Exception:
            continue

    if not metadata_files:
        print("No orchestrator metadata found — nothing to stop.")
        return 0

    sig = signal.SIGKILL if args.force else signal.SIGTERM
    sig_name = "SIGKILL" if args.force else "SIGTERM"
    timeout = args.timeout

    stopped = 0
    cleaned = 0
    errors = 0

    print(f"Stopping all orchestrator daemons ({len(metadata_files)} metadata files found)...")
    print()

    for meta_path, meta in metadata_files:
        pid = meta.get("pid")
        slug = meta.get("project_slug", meta_path.parent.name)
        ws = meta.get("workspace_root", "?")

        if pid is None or not _is_pid_alive(pid):
            pid_str = pid or "N/A"
            print(f"  [{slug}] already stopped (PID {pid_str}) — cleaning up stale metadata")
            try:
                meta_path.unlink(missing_ok=True)
                cleaned += 1
            except OSError as exc:
                print(f"    ⚠ failed to clean metadata: {exc}")
                errors += 1
            continue

        # Send signal
        print(f"  [{slug}] stopping daemon (PID {pid}, workspace: {ws})...")
        print(f"    Sending {sig_name}...")
        try:
            os.kill(pid, sig)
        except ProcessLookupError:
            print(f"    Process {pid} already exited.")
        except PermissionError:
            print(f"    ⚠ Permission denied — cannot signal PID {pid}.", file=sys.stderr)
            errors += 1
            continue

        # Wait for graceful shutdown (non-force only)
        if not args.force:
            deadline = time.time() + timeout
            while time.time() < deadline:
                if not _is_pid_alive(pid):
                    break
                time.sleep(0.2)
            else:
                print(
                    f"    ⚠ Process did not exit within {timeout}s timeout. Remove --force or kill manually: kill -9 {pid}"
                )
                errors += 1
                # Still clean up metadata
        else:
            # Brief pause so SIGKILL takes effect
            time.sleep(0.3)

        try:
            meta_path.unlink(missing_ok=True)
            stopped += 1
        except OSError as exc:
            print(f"    ⚠ failed to clean metadata: {exc}")
            errors += 1

    print()
    print(f"Done: {stopped} stopped, {cleaned} stale cleaned, {errors} error(s).")
    return 1 if errors else 0


def _run_stop(args: argparse.Namespace) -> int:
    """Stop the orchestrator daemon. Idempotent — already-stopped → exit 0."""
    if getattr(args, "all", False):
        return _run_stop_all(args)

    meta_path, meta = _find_metadata(args)

    if meta is None:
        print("Orchestrator daemon: already stopped (no metadata found)")
        return 0  # idempotent

    pid = meta.get("pid")
    started_at = meta.get("started_at", 0)
    project_slug = meta.get("project_slug", "unknown")
    workspace_root = meta.get("workspace_root", "unknown")

    if pid is None or not _is_pid_alive(pid):
        print(f"Orchestrator daemon: already stopped (PID {pid or 'N/A'} not running)")
        # Clean up stale metadata
        if meta_path and meta_path.exists():
            meta_path.unlink()
            print("  Stale metadata cleaned up.")
        return 0  # idempotent

    # Send stop signal
    sig = signal.SIGKILL if args.force else signal.SIGTERM
    sig_name = "SIGKILL" if args.force else "SIGTERM"
    print(f"Stopping orchestrator daemon (PID {pid}, project: {project_slug})...")
    print(f"  Sending {sig_name}...")

    try:
        os.kill(pid, sig)
    except ProcessLookupError:
        print(f"  Process {pid} already exited.")
    except PermissionError:
        print(f"  Permission denied: cannot signal PID {pid}.", file=sys.stderr)
        print(
            f"  Try running with elevated privileges or kill manually: kill {pid}", file=sys.stderr
        )
        return 1

    # If not force, wait for graceful shutdown
    if not args.force:
        timeout = args.timeout
        deadline = time.time() + timeout
        while time.time() < deadline:
            if not _is_pid_alive(pid):
                break
            time.sleep(0.2)
        else:
            # Timed out — process still alive
            print(f"  Process did not exit within {timeout}s timeout. Use --force for SIGKILL.")
            print(f"  You may also kill manually: kill -9 {pid}")
            return 1

    # Clean up metadata
    if meta_path and meta_path.exists():
        meta_path.unlink()
        print(f"  Metadata cleaned up: {meta_path}")

    print("Orchestrator daemon stopped.")
    return 0


# ---------------------------------------------------------------------------
# server start
# ---------------------------------------------------------------------------


def _run_connect_gateway(args: argparse.Namespace) -> int:
    """Submit an IM gateway connect request to the running orchestrator daemon."""
    origin = _resolve_gateway_origin(args)

    meta_path, meta = _find_metadata(args)
    pid = meta.get("pid") if meta else None
    try:
        alive = bool(pid and _is_pid_alive(int(pid)))
    except (TypeError, ValueError):
        alive = False
    if not alive:
        print("连接失败，orchestrator未启动", file=sys.stderr)
        return 1

    sock = _resolve_gateway_sock(args)
    if not _gateway_socket_available(sock):
        print("IM gateway daemon is not running", file=sys.stderr)
        print(f"  Requested socket: {sock}", file=sys.stderr)
        return 1

    workspace = Path(meta.get("workspace_root", os.getcwd())) if meta else Path.cwd()
    response_path = _gateway_control_response_path(workspace, "gateway_connect")
    control_path = _write_gateway_control(
        workspace,
        "gateway_connect",
        {
            "origin": origin,
            "sock": sock,
            "response_path": str(response_path),
        },
    )
    result = _wait_gateway_control_result(response_path)
    if result is not None:
        if result.get("ok"):
            print(f"gateway connected: origin={origin} sock={sock}")
            return 0
        print(
            f"gateway connect failed: {result.get('message') or 'unknown error'}", file=sys.stderr
        )
        return 1

    print("gateway connect request submitted; waiting for orchestrator next poll")
    print(f"  Control: {control_path}")
    print(f"  Running daemon PID: {pid}")
    if meta_path:
        print(f"  Metadata: {meta_path}")
    return 0


def _run_disconnect_gateway(args: argparse.Namespace) -> int:
    """Submit an IM gateway disconnect request to the running orchestrator daemon."""
    meta_path, meta = _find_metadata(args)
    pid = meta.get("pid") if meta else None
    try:
        alive = bool(pid and _is_pid_alive(int(pid)))
    except (TypeError, ValueError):
        alive = False
    if not alive:
        print("连接失败，orchestrator未启动", file=sys.stderr)
        return 1

    workspace = Path(meta.get("workspace_root", os.getcwd())) if meta else Path.cwd()
    response_path = _gateway_control_response_path(workspace, "gateway_disconnect")
    control_path = _write_gateway_control(
        workspace,
        "gateway_disconnect",
        {
            "response_path": str(response_path),
        },
    )
    result = _wait_gateway_control_result(response_path)
    if result is not None:
        if result.get("ok"):
            print("gateway disconnected")
            return 0
        print(
            f"gateway disconnect failed: {result.get('message') or 'unknown error'}",
            file=sys.stderr,
        )
        return 1

    print("gateway disconnect request submitted; waiting for orchestrator next poll")
    print(f"  Control: {control_path}")
    print(f"  Running daemon PID: {pid}")
    if meta_path:
        print(f"  Metadata: {meta_path}")
    return 0


def _resolve_gateway_origin(args: argparse.Namespace) -> str:
    explicit = getattr(args, "gateway", None)
    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip()
    origin = os.environ.get("ORCHESTRATORD_GATEWAY_ORIGIN")
    if origin:
        return origin
    from orchestratord.ipc import IM_DIRECT_ALL_ORIGIN

    return IM_DIRECT_ALL_ORIGIN


def _resolve_gateway_sock(args: argparse.Namespace) -> str:
    sock = (
        getattr(args, "gateway_sock", None)
        or os.environ.get("ORCHESTRATORD_GATEWAY_SOCK")
        or os.environ.get("ORCHESTRATORD_IM_GATEWAY_SOCK")
    )
    return str(sock or GATEWAY_SOCK)


def _gateway_socket_available(sock: str) -> bool:
    if not Path(sock).exists():
        return False
    try:
        asyncio.run(_probe_gateway_socket(sock))
        return True
    except Exception:  # noqa: BLE001
        return False


async def _probe_gateway_socket(sock: str) -> None:
    from orchestratord.ipc.client import GatewayIpcClient

    client = GatewayIpcClient(sock, instance_id="orchestrator-control-probe")
    try:
        await client.connect()
    finally:
        await client.close()


def _gateway_control_response_path(workspace: Path, command: str) -> Path:
    control_dir = workspace / ".orchestrator_control"
    control_dir.mkdir(parents=True, exist_ok=True)
    return control_dir / f"{command}_{uuid.uuid4().hex}.result.json"


def _write_gateway_control(workspace: Path, command: str, payload: dict) -> Path:
    control_dir = workspace / ".orchestrator_control"
    control_dir.mkdir(parents=True, exist_ok=True)
    request_id = uuid.uuid4().hex
    control_path = control_dir / f"{command}_{request_id}.control"
    body = f"{command}\n\n{json.dumps(payload, ensure_ascii=False, separators=(',', ':'))}\n"
    control_path.write_text(body, encoding="utf-8")
    return control_path


def _wait_gateway_control_result(response_path: Path, timeout_seconds: float = 0.2) -> dict | None:
    deadline = time.time() + max(0.0, timeout_seconds)
    while time.time() < deadline:
        if response_path.exists():
            try:
                return json.loads(response_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                return {"ok": False, "message": "invalid gateway control result"}
        time.sleep(0.02)
    return None


def _run_start(args: argparse.Namespace) -> int:
    """Start the orchestrator daemon. Idempotent — already-running → show status."""
    # Check if already running
    meta_path, meta = _find_metadata(args)
    if meta:
        pid = meta.get("pid")
        if pid and _is_pid_alive(pid):
            print(f"Orchestrator daemon is already running (PID {pid}).")
            print("Showing current status:")
            return _run_status(args)
        # Clean up stale metadata from dead PID before starting fresh
        if meta_path and meta_path.exists():
            meta_path.unlink(missing_ok=True)
            print(f"  Cleaned stale metadata from dead PID {pid or 'N/A'}")

    # Launch the orchestrator directly
    return _run_orchestrator(
        workflow_path=args.workflow,
        dashboard=getattr(args, "dashboard", False),
        port=getattr(args, "port", None),
        workflow_yaml_path=getattr(args, "workflow_yaml", None),
        gateway=getattr(args, "gateway", False),
        gateway_origin=getattr(args, "gateway_origin", None),
        gateway_sock=getattr(args, "gateway_sock", None),
        backend=getattr(args, "backend", None),
    )


# ---------------------------------------------------------------------------
# orchestrator launch
# ---------------------------------------------------------------------------


def _mount_gateway_opt_in(
    subsystem,
    config,
    *,
    enabled: bool = False,
    origin: str | None = None,
    sock: str | None = None,
    feishu_adapter: Any | None = None,
):
    """Connect the orchestrator daemon to the IM gateway (opt-in via env).

    Enabled when ``enabled`` is true or ``ORCHESTRATORD_GATEWAY_ORIGIN`` is set.
    Without a specific origin, this binds all supported direct/private IM messages.
    Returns the
    :class:`OrchestratorGatewayClient` (for heartbeat scheduling) or None.

    Inbound IM messages for the origin are pushed over IPC and dispatched
    to existing orchestrator entry points; orchestrator events flow back to
    IM via OUTBOUND frames (``build_ipc_deliver``). No behavior change
    when the env var is unset.
    """
    import os

    origin = origin or os.environ.get("ORCHESTRATORD_GATEWAY_ORIGIN")
    if not origin and enabled:
        from orchestratord.ipc import IM_DIRECT_ALL_ORIGIN

        origin = IM_DIRECT_ALL_ORIGIN
    if not origin:
        return None
    sock = (
        sock
        or os.environ.get("ORCHESTRATORD_GATEWAY_SOCK")
        or os.environ.get("ORCHESTRATORD_IM_GATEWAY_SOCK")
        or os.environ.get("ORCHESTRATORD_GATEWAY_SOCK")
    )
    if not sock:
        sock = str(GATEWAY_SOCK)

    from orchestratord.im_gateway_client import (
        OrchestratorGatewayClient,
        OrchestratorHandlers,
    )

    def _orch():
        # subsystem._orchestrator is built during run(); resolve lazily.
        return getattr(subsystem, "_orchestrator", None)

    def _control_verb(verb, issue_id):
        o = _orch()
        if o is not None and hasattr(o, "_apply_control_command"):
            try:
                o._apply_control_command(verb, issue_id or "", "")
                logger.info("IM control_verb: %s issue=%s", verb, issue_id)
                return
            except Exception:
                logger.exception("IM control_verb failed")
        logger.warning("IM control_verb: orchestrator not ready (%s %s)", verb, issue_id)

    def _issue_inject(issue_id, hint):
        # Write to the workspace's .operator_hints.md via the orchestrator.
        o = _orch()
        ws_root = getattr(getattr(config, "workspace", None), "root", "")
        if ws_root:
            try:
                from pathlib import Path

                hints_file = Path(ws_root) / ".operator_hints.md"
                hints_file.parent.mkdir(parents=True, exist_ok=True)
                with hints_file.open("a", encoding="utf-8") as f:
                    f.write(f"\n{hint}\n")
                logger.info("IM issue_inject: issue=%s hint_len=%d", issue_id, len(hint))
                return
            except Exception:
                logger.exception("IM issue_inject failed")
        logger.warning("IM issue_inject: no workspace root")

    def _operator_hints(issue_id, text):
        _issue_inject(issue_id, text)

    def _queue_pending(issue_id, text):
        # Pending-message queue lives on RuntimeTaskRegistry; without an
        # active task for this issue we record the intent for the next run.
        logger.info("IM followup queued: issue=%s text_len=%d", issue_id, len(text))

    def _agent_intent(verb, issue_id):
        _control_verb(verb, issue_id)

    def _issue_cli(verb, issue_id, payload):
        logger.info("IM issue_cli: %s issue=%s", verb, issue_id)

    def _bridge_interrupt(issue_id, payload):
        _control_verb("stop", issue_id)

    handlers = OrchestratorHandlers(
        queue_pending_message=_queue_pending,
        control_verb=_control_verb,
        issue_inject=_issue_inject,
        operator_hints=_operator_hints,
        agent_intent=_agent_intent,
        issue_cli=_issue_cli,
        bridge_interrupt=_bridge_interrupt,
    )

    from orchestratord.ipc.client import GatewayIpcClient

    session_id = f"orchestrator-{os.getpid()}"
    ipc = GatewayIpcClient(sock, instance_id=session_id)
    wrapper = OrchestratorGatewayClient(
        handlers, ipc_client=ipc, origin=origin, command_router=None, control_bridge=None
    )

    async def _connect_and_register() -> bool:
        """Connect to the gateway and register. Returns True on success.

        Never raises — the gateway and orchestrator are decoupled and
        either may be stopped independently. When the gateway is
        unavailable, returns False so the caller can retry on the next
        heartbeat without printing a traceback.
        """
        try:
            response = await ipc.reconnect_until_registered(
                session_id=session_id,
                origin=origin,
                capabilities=["outbound_text"],
            )
        except Exception:  # noqa: BLE001
            logger.debug("orchestrator IM reconnect raised (gateway unavailable)")
            return False
        if response is None or response.ack_layer != "accepted":
            logger.warning("orchestrator IM gateway unavailable; will retry on next heartbeat")
            return False
        flush_pending = getattr(wrapper, "_flush_pending_outbound", None)
        if callable(flush_pending):
            await flush_pending()
        logger.info("orchestrator IM opt-in connected: origin=%s sock=%s", origin[:32], sock)
        return True

    async def _heartbeat_loop():
        # Connect first, then heartbeat every 30s.  Startup can race the
        # gateway daemon, so keep trying instead of silently disabling IM.
        while not await _connect_and_register():
            await asyncio.sleep(30.0)
        missed_heartbeats = 0
        while True:
            try:
                response = await ipc.heartbeat()
                if response is None:
                    missed_heartbeats += 1
                    if missed_heartbeats < 2:
                        logger.warning(
                            "orchestrator IM heartbeat ACK timed out; "
                            "keeping the current registration until the next check"
                        )
                    else:
                        logger.warning("orchestrator IM heartbeat timed out twice; reconnecting")
                        await _connect_and_register()
                        missed_heartbeats = 0
                elif response.ack_layer != "accepted":
                    logger.warning("orchestrator IM heartbeat was not accepted; reconnecting")
                    await _connect_and_register()
                    missed_heartbeats = 0
                else:
                    missed_heartbeats = 0
                    maybe_flush = getattr(wrapper, "_flush_pending_outbound", None)
                    if callable(maybe_flush):
                        await maybe_flush()
            except Exception:  # noqa: BLE001
                logger.warning("orchestrator IM heartbeat failed; reconnecting")
                await _connect_and_register()
            await asyncio.sleep(30.0)

    wrapper._heartbeat_loop = _heartbeat_loop

    # Outbound: orchestrator events → WeChat via OUTBOUND frames.
    # _build_session_sink reads im_event_deliver at sink-build time inside
    # Orchestrator.run(); set it on the orchestrator instance right after
    # subsystem.run() constructs it, before it starts polling.
    def _sync_deliver(event, text):
        try:
            loop = asyncio.get_event_loop()
            loop.create_task(wrapper.send_outbound(text))
        except RuntimeError:
            logger.warning("orchestrator IM: no loop; dropping event")

    _orig_run = subsystem.run

    async def _run_with_im():
        # subsystem.run constructs self._orchestrator then calls its run().
        # Patch run() so we set im_event_deliver on the orchestrator before
        # it starts polling / building session sinks.
        from orchestratord.orchestrator import Orchestrator as _Orch

        _orig_orch_run = _Orch.run

        async def _orch_run_patched(self, *a, **kw):
            self._im_gateway_wrapper = wrapper
            self._im_gateway_session_id = session_id
            self._im_gateway_heartbeat_task = getattr(wrapper, "_heartbeat_task", None)
            self.im_event_deliver = _sync_deliver
            self.im_event_channel = "wechat"
            # F-??? Feishu activity-sink wiring: when the caller passes
            # a FeishuAppChannelAdapter, propagate it through to the
            # orchestrator so :meth:`Orchestrator._build_session_sink`
            # can attach a :class:`FeishuActivitySink` per session. Stays
            # a no-op when ``feishu_adapter`` is None.
            if feishu_adapter is not None:
                self.im_channel_adapter = feishu_adapter
            if hasattr(self, "_emit_im_event"):
                from orchestratord.events import EventLevel

                self._emit_im_event(
                    "",
                    "orchestrator.started",
                    EventLevel.INFO,
                    "IM notifications enabled",
                )
            return await _orig_orch_run(self, *a, **kw)

        _Orch.run = _orch_run_patched
        try:
            await _orig_run()
        finally:
            _Orch.run = _orig_orch_run

    subsystem.run = _run_with_im

    return wrapper


def _run_orchestrator(
    workflow_path: str | None,
    dashboard: bool = False,
    port: int | None = None,
    workflow_yaml_path: str | None = None,
    gateway: bool = False,
    gateway_origin: str | None = None,
    gateway_sock: str | None = None,
    backend: str | None = None,
) -> int:
    """Launch the orchestrator with a workflow file.

    This is the core launch entry point. Supports optional embedded
    dashboard status printing.
    """
    import asyncio
    import logging

    from orchestratord.tracker import TrackerConfigError, validate_tracker_config
    from orchestratord.workflow import WorkflowLoader, WorkflowParseError

    if not workflow_path:
        print("error: --workflow is required", file=sys.stderr)
        return 2

    try:
        config, prompt = WorkflowLoader.load(workflow_path)
    except WorkflowParseError as exc:
        print(f"error: failed to parse workflow: {exc}", file=sys.stderr)
        return 2
    except FileNotFoundError:
        print(f"error: workflow file not found: {workflow_path}", file=sys.stderr)
        return 2

    # Load prompt into WorkflowStore so PromptBuilder can use it
    from ..workflow_store import get_workflow_store

    get_workflow_store().load(workflow_path)

    try:
        validate_tracker_config(config.tracker)
    except TrackerConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    # The orchestrator daemon is a long-running process whose INFO logs
    # (poll ticks, issue lifecycle, retries) are its primary diagnostic
    # surface. Use the centralized logging setup for consistent format,
    # timezone-aware timestamps, MDC context injection, and optional
    # JSON output for log aggregators.
    _ws_root = getattr(config.workspace, "root", "") or ""
    _json_log = str(Path(_ws_root) / ".reports" / "orchestrator.ndjson") if _ws_root else None
    from ..logging_setup import configure_orchestrator_logging

    configure_orchestrator_logging(
        level=logging.INFO,
        json_path=_json_log,
    )

    # Build repo slug for the startup banner
    _tracker_kind = getattr(config.tracker, "kind", "?")
    _owner = getattr(config.tracker, "owner", None) or ""
    _repo = getattr(config.tracker, "repo", None) or ""
    _repo_slug = f"{_owner}/{_repo}" if _owner and _repo else ""
    _pid = os.getpid()
    _agent = getattr(config, "agent", None)

    print(f"\u2713 orchestrator daemon started \u00b7 pid {_pid}", end="")
    if _tracker_kind and _tracker_kind != "?":
        print(f" \u00b7 tracker={_tracker_kind}", end="")
        if _repo_slug:
            print(f" \u00b7 repo={_repo_slug}", end="")
    print()
    if _agent is not None:
        print(
            f"\u2713 max_concurrent_agents={getattr(_agent, 'max_concurrent_agents', '?')}"
            f" \u00b7 permission_mode={getattr(_agent, 'permission_mode', '?')}"
        )

    from orchestratord.applications import IssueToPrApplication

    # Validate only the selected SPI backend. Discovery keeps legacy backend
    # backend names stable while pre-flight runs
    # before any session is created.
    spi_backend = None
    if backend is not None:
        from orchestratord.backend_registry import discover_backends
        from orchestratord.spi.backend import SessionSpec

        backends = discover_backends()
        if backend not in backends:
            available = ", ".join(sorted(backends.keys()))
            print(
                f"error: backend '{backend}' not found. Available: {available}",
                file=sys.stderr,
            )
            return 2
        spi_backend = backends[backend]

        agent = config.agent
        # Mirror BackendRunner._build_session_spec: forward the provider
        # route registry so startup preflight validates the same spec
        # shape a run will use (empty registry → no extra channel change).
        from ..backend_runner import providers_extra

        spec = SessionSpec(
            cwd=str(getattr(config.workspace, "root", "") or "."),
            provider=getattr(agent, "provider", None),
            model=getattr(agent, "model", None),
            base_url=getattr(agent, "base_url", None),
            api_key=getattr(agent, "api_key", None),
            cordis=getattr(agent, "cordis", None),
            runtime_bin=getattr(agent, "runtime_bin", None),
            env=getattr(agent, "env", None) or {},
            extra=providers_extra(agent),
        )
        try:
            spi_backend.preflight(spec)
        except RuntimeError as exc:
            print(
                f"error: backend '{backend}' pre-flight failed: {exc}",
                file=sys.stderr,
            )
            return 2
        print(f"  backend={backend} ({spi_backend.display_name})")

    subsystem = IssueToPrApplication(
        config, workflow_yaml_path=workflow_yaml_path, backend=spi_backend
    )

    # Fix 2: write the real daemon PID to <workspace>/daemon.pid
    # so external tools (cron monitor, stop scripts) can locate the
    # running daemon.  The previous shell-wrapper pattern
    # ``nohup ... & disown; echo $! > pidfile`` captured the nohup
    # wrapper PID which sometimes did not match the python process
    # that ultimately ran the orchestrator (chain-exec races, signal
    # forwarding).  Writing the pidfile in-process via ``os.getpid()``
    # makes the value authoritative and removes the dependency on
    # the shell launcher's PID semantics.
    try:
        import atexit

        _ws_root = Path(getattr(config.workspace, "root", "") or "")
        if str(_ws_root):
            _pidfile = _ws_root / "daemon.pid"
            _pidfile.parent.mkdir(parents=True, exist_ok=True)
            _pidfile.write_text(f"{os.getpid()}\n", encoding="utf-8")

            def _cleanup_pidfile() -> None:
                try:
                    _pidfile.unlink(missing_ok=True)
                except Exception:
                    pass

            atexit.register(_cleanup_pidfile)
    except Exception as exc:  # noqa: BLE001
        # Never block daemon start on pidfile failures (read-only
        # workspace, missing dir, etc.) — just warn and continue.
        print(
            f"warning: failed to write pidfile: {exc}",
            file=sys.stderr,
        )

    # Register signal handlers for graceful shutdown on SIGTERM/SIGINT.
    # Without these, a plain `kill <pid>` or Ctrl+C sends SIGTERM/SIGINT
    # which Python asyncio does not handle — the process dies immediately
    # without running any cleanup (shutdown(), _cancel_all_tasks(), atexit).
    # With signal handlers, the event loop catches the signal and calls
    # subsystem.shutdown(), which sets _shutdown_event so the polling loop
    # exits cleanly, then _cancel_all_tasks() cancels running issues.
    #
    # SIGKILL (-9) cannot be caught and will still cause abrupt death;
    # the pdeath_sig PR_SET_PDEATHSIG in subprocesses mitigates orphan
    # children for that case.
    #
    # IMPORTANT: signal handlers MUST be registered on the loop that
    # actually runs subsystem.run(). Using asyncio.get_event_loop() before
    # asyncio.run() grabs a stale/ghost loop (asyncio.run creates a new
    # one internally), so the handler would never fire. We register inside
    # the coroutine via get_running_loop() to bind to the real running loop.

    def _schedule_shutdown(sig_name: str) -> None:
        """Callback registered via loop.add_signal_handler."""
        logger.info("Received %s — scheduling graceful shutdown...", sig_name)
        # Schedule the async shutdown as a task; add_signal_handler
        # only accepts synchronous callables.
        asyncio.create_task(subsystem.shutdown())

    # IM gateway opt-in: when configured, register the orchestrator as the
    # opt-in target for that WeChat origin so inbound messages drive
    # orchestrator actions, and orchestrator events flow back to WeChat via
    # OUTBOUND IPC frames. No-op otherwise.
    im_client_wrapper = _mount_gateway_opt_in(
        subsystem,
        config,
        enabled=gateway,
        origin=gateway_origin,
        sock=gateway_sock,
    )

    async def _run() -> None:
        # Bind signal handlers to the loop that is actually running this
        # coroutine. asyncio.run() creates a fresh loop, so registration
        # must happen here, not outside asyncio.run().
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(
                    sig,
                    lambda sig_name=signal.Signals(sig).name: _schedule_shutdown(sig_name),
                )
            except NotImplementedError:
                # Windows ProactorEventLoop has no POSIX signal support.
                # The daemon can still run and clean up through normal
                # cancellation/atexit paths.
                break
        im_task = None
        if im_client_wrapper is not None:
            im_task = asyncio.create_task(im_client_wrapper._heartbeat_loop())
            im_client_wrapper._heartbeat_task = im_task
        try:
            await subsystem.run()
        except (asyncio.CancelledError, KeyboardInterrupt):
            await subsystem.shutdown()
            raise
        finally:
            if im_task is not None and not im_task.done():
                im_task.cancel()
                with __import__("contextlib").suppress(asyncio.CancelledError):
                    await im_task

    if dashboard:

        async def _run_with_dashboard() -> None:
            """Run orchestrator with a concurrent dashboard status loop."""
            dashboard_task = asyncio.create_task(_dashboard_loop(subsystem.status_dashboard, port))
            try:
                await _run()
            finally:
                dashboard_task.cancel()

        asyncio.run(_run_with_dashboard())
    else:
        asyncio.run(_run())

    return 0


async def _dashboard_loop(dashboard, port: int | None) -> None:
    """Periodic dashboard status print loop."""

    while True:
        await asyncio.sleep(5)
        try:
            state = dashboard.state()
            running_ids = list(state.get("running", {}).keys())
            print(
                f"[dashboard] running={len(running_ids)} "
                f"completed={state.get('completed_count', 0)} "
                f"failed={state.get('failed_count', 0)}",
                file=sys.stderr,
            )
        except Exception:
            pass
