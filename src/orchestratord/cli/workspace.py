"""``orchestratord workspace`` subcommands — operator-facing workspace diagnostics.

This module is intentionally minimal. The workspace lifecycle itself is owned
by ``orchestratord.workspace`` (the daemon-side :class:`WorkspaceManager`).
The CLI surface here only exposes read-only diagnostics that operators reach
for during incident triage — listing preserved workspaces and inspecting the
on-disk state of one. Heavy workspace mutations stay on the daemon side and
are never invoked directly from the CLI.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


def add_workspace_parser(subparsers: argparse._SubParsersAction) -> None:
    """Register the ``workspace`` subcommand and its children."""
    workspace_parser = subparsers.add_parser(
        "workspace",
        help="Inspect preserved workspaces and per-issue metadata",
        description=(
            "Read-only diagnostics for workspace state. The daemon owns "
            "workspace lifecycle mutations; this CLI does not expose them."
        ),
    )
    workspace_sub = workspace_parser.add_subparsers(
        dest="workspace_subcommand",
        required=True,
    )
    workspace_sub.add_parser(
        "list",
        help="List preserved workspaces under the configured workspace root",
    )
    show = workspace_sub.add_parser(
        "show",
        help="Show metadata for one workspace",
    )
    show.add_argument(
        "issue_id",
        type=str,
        help="Issue identifier (kebab-case, matches the tracker label)",
    )


def run(args: argparse.Namespace) -> int:
    """Dispatch the chosen ``workspace`` subcommand."""
    if args.workspace_subcommand == "list":
        return _run_list()
    if args.workspace_subcommand == "show":
        return _run_show(args.issue_id)
    print(
        f"Unknown workspace subcommand: {args.workspace_subcommand}",
        file=sys.stderr,
    )
    return 2


def _resolve_workspace_root() -> Path | None:
    """Best-effort lookup of the workspace root from env."""
    env_root = os.environ.get("ORCHESTRATORD_WORKSPACE_ROOT")
    if env_root:
        return Path(env_root).expanduser().resolve()
    return None


def _run_list() -> int:
    root = _resolve_workspace_root()
    if root is None or not root.exists():
        print(
            "workspace root not configured or missing "
            "(set ORCHESTRATORD_WORKSPACE_ROOT)",
            file=sys.stderr,
        )
        return 1
    entries = sorted(p for p in root.iterdir() if p.is_dir())
    if not entries:
        print(f"(no workspaces under {root})")
        return 0
    print(f"{'ISSUE_ID':<32} PATH")
    for entry in entries:
        print(f"{entry.name:<32} {entry}")
    return 0


def _run_show(issue_id: str) -> int:
    root = _resolve_workspace_root()
    if root is None:
        print("workspace root not configured", file=sys.stderr)
        return 1
    workspace = root / issue_id
    metadata_path = workspace / ".orchestrator_workspace" / "metadata.json"
    if not workspace.exists():
        print(f"workspace not found: {workspace}", file=sys.stderr)
        return 1
    if not metadata_path.exists():
        print(
            f"workspace exists but has no metadata.json: {workspace}",
            file=sys.stderr,
        )
        return 1
    print(json.dumps(json.loads(metadata_path.read_text()), indent=2))
    return 0