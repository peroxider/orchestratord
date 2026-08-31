"""orchestrator issue — manage individual issues handled by the orchestrator.

Usage (noun-verb, all using self-describing ``--id`` parameters):

  # Query
  orchestratord issue list [--status <filter>]
  orchestratord issue show --id <id>
  orchestratord issue tail --id <id>

  # Lifecycle
  orchestratord issue stop --id <id>
  orchestratord issue pause --id <id> [--reason <text>]
  orchestratord issue resume --id <id>
  # Note: takeover is a runtime control-socket verb (handled in
  # runner_utils.py), not a separate CLI subcommand. It is dispatched
  # through control_socket events, not through ``orchestratord issue``.

  # Operator interaction
  orchestratord issue clarify --id <id> --answer <text> [--forward-to-author]
  orchestratord issue inject --id <id> <hint> [--list] [--remove N]

  # Workspace
  orchestratord issue workspace --id <id> [--ls] [--cat FILE] [--edit FILE --with CONTENT]

Design principles:
  - Self-describing parameters: use ``--id <id>`` instead of positional ``issue_id``
  - All commands are idempotent where possible
  - Stable behaviour: same args produce same outcome (or equivalent no-op)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

from orchestratord.paths import SESSIONS_DIR


# ---------------------------------------------------------------------------
# Parser registration
# ---------------------------------------------------------------------------


def add_issue_parser(
    subparsers: argparse._SubParsersAction,
    *,
    command_name: str = "issue",
) -> None:
    """Register ``issue`` sub-subcommands."""
    issue_parser = subparsers.add_parser(
        command_name,
        help="Manage individual issues handled by the orchestrator",
        description="List, show, tail, stop, pause, resume, clarify, "
        "inject, or view workspace of issues managed by the orchestrator. "
        "All issue-level commands use --id for self-describing parameters "
        "and are designed to be idempotent.",
    )
    issue_sub = issue_parser.add_subparsers(
        dest="issue_subcommand",
        required=True,
    )

    # --- issue list ---
    list_parser = issue_sub.add_parser(
        "list",
        help="List all issues with their status",
        description="Display all issues known to the orchestrator, optionally "
        "filtered by status. Idempotent (pure read).",
    )
    list_parser.add_argument(
        "--status",
        choices=["pending", "running", "synced", "completed", "failed", "abandoned"],
        help="Filter by issue status",
    )
    list_parser.add_argument(
        "--workspace",
        type=str,
        default=None,
        metavar="PATH",
        help="Explicit workspace root path (optional auto-detection override)",
    )
    list_parser.add_argument(
        "--workflow",
        type=str,
        default=None,
        metavar="PATH",
        help="Path to WORKFLOW.md (resolution hint when metadata is missing)",
    )

    # --- issue show ---
    show_parser = issue_sub.add_parser(
        "show",
        help="Show details for a specific issue",
        description="Display issue metadata: status, branch, PR, token usage, "
        "and workspace path. Idempotent (pure read).",
    )
    show_parser.add_argument(
        "--id",
        type=str,
        required=True,
        metavar="ISSUE_ID",
        help="Issue identifier (e.g. 42 or owner/repo#42)",
    )
    show_parser.add_argument(
        "--workspace",
        type=str,
        default=None,
        metavar="PATH",
        help="Explicit workspace root path",
    )
    show_parser.add_argument(
        "--workflow",
        type=str,
        default=None,
        metavar="PATH",
        help="Path to WORKFLOW.md (resolution hint when metadata is missing)",
    )

    # --- issue tail ---
    tail_parser = issue_sub.add_parser(
        "tail",
        help="Tail tool call logs for a running issue in real-time",
        description="Stream tool call events from a running issue's event log. "
        "Idempotent (pure read, non-destructive).",
    )
    tail_parser.add_argument(
        "--id",
        type=str,
        required=True,
        metavar="ISSUE_ID",
        help="Issue identifier to tail",
    )
    tail_parser.add_argument(
        "--workspace",
        type=str,
        default=None,
        metavar="PATH",
        help="Explicit workspace root path",
    )
    tail_parser.add_argument(
        "--turn",
        type=int,
        default=None,
        metavar="N",
        help="Filter to show only events from turn number N",
    )

    # --- issue transcript ---
    transcript_parser = issue_sub.add_parser(
        "transcript",
        help="Print a session transcript for an issue or run",
        description="Read the full session transcript from "
        "~/.orchestratord/sessions/{run_id}/transcript.jsonl and "
        "print it as text. Idempotent (pure read, suitable for "
        "piping).",
    )
    transcript_parser.add_argument(
        "--id",
        type=str,
        default=None,
        metavar="ISSUE_ID",
        help="Issue identifier (resolves to run_id via the registry)",
    )
    transcript_parser.add_argument(
        "--run",
        type=str,
        default=None,
        metavar="RUN_ID",
        help="Run identifier (skips registry resolution)",
    )
    transcript_parser.add_argument(
        "--workspace",
        type=str,
        default=None,
        metavar="PATH",
        help="Explicit workspace root path",
    )
    transcript_parser.add_argument(
        "--workflow",
        type=str,
        default=None,
        metavar="PATH",
        help="Path to WORKFLOW.md (resolution hint when metadata is missing)",
    )
    transcript_parser.add_argument(
        "--role",
        choices=["user", "assistant"],
        default=None,
        help="Filter to show only messages with this role",
    )
    transcript_parser.add_argument(
        "--tool-use-id",
        dest="tool_use_id",
        type=str,
        default=None,
        metavar="TOOL_USE_ID",
        help="Filter to show only tool_use / tool_result blocks with this id",
    )
    transcript_parser.add_argument(
        "--limit",
        type=int,
        default=None,
        metavar="N",
        help="Limit to the first N messages",
    )

    # --- issue stop ---
    stop_parser = issue_sub.add_parser(
        "stop",
        help="Force-terminate a running agent for an issue",
        description="Write a stop control command for the orchestrator to pick up "
        "on its next poll cycle. The agent will be marked as failed. "
        "Idempotent: stopping an already-stopped issue succeeds silently.",
    )
    stop_parser.add_argument(
        "--id",
        type=str,
        required=True,
        metavar="ISSUE_ID",
        help="Issue identifier to stop",
    )
    stop_parser.add_argument(
        "--yes",
        "-y",
        action="store_true",
        default=False,
        help="Skip confirmation prompt",
    )
    stop_parser.add_argument(
        "--no-wait",
        dest="no_wait",
        action="store_true",
        default=False,
        help="Send stop and return immediately without waiting for agent to terminate",
    )

    # --- issue pause ---
    pause_parser = issue_sub.add_parser(
        "pause",
        help="Pause a running agent at the next tool call boundary",
        description="Write a pause control command. The agent will complete its "
        "current tool call then pause (no new tool calls until resume). "
        "Idempotent: pausing an already-paused issue succeeds silently.",
    )
    pause_parser.add_argument(
        "--id",
        type=str,
        required=True,
        metavar="ISSUE_ID",
        help="Issue identifier to pause",
    )
    pause_parser.add_argument(
        "--reason",
        type=str,
        default="",
        help="Reason for pausing (visible to the agent)",
    )
    pause_parser.add_argument(
        "--no-wait",
        dest="no_wait",
        action="store_true",
        default=False,
        help="Send pause and return immediately without waiting for confirmation",
    )

    # --- issue resume ---
    resume_parser = issue_sub.add_parser(
        "resume",
        help="Resume a paused agent",
        description="Write a resume control command to allow the agent to continue. "
        "Idempotent: resuming a running (non-paused) issue succeeds silently.",
    )
    resume_parser.add_argument(
        "--id",
        type=str,
        required=True,
        metavar="ISSUE_ID",
        help="Issue identifier to resume",
    )
    resume_parser.add_argument(
        "--no-wait",
        dest="no_wait",
        action="store_true",
        default=False,
        help="Send resume and return immediately without waiting for confirmation",
    )

    # --- issue clarify ---
    clarify_parser = issue_sub.add_parser(
        "clarify",
        help="Answer a clarification request from the orchestrator",
        description="Record an operator answer for a pending clarification. "
        "The orchestrator picks up the answer on its next poll cycle. "
        "Idempotent: answering an already-answered clarification "
        "updates the answer in place.",
    )
    clarify_parser.add_argument(
        "--id",
        type=str,
        required=False,
        metavar="ISSUE_ID",
        help="Issue ID being clarified",
    )
    clarify_parser.add_argument(
        "--answer",
        type=str,
        default=None,
        help="Operator's answer to the clarification question",
    )
    clarify_parser.add_argument(
        "--forward-to-author",
        action="store_true",
        help="Skip local answer, forward directly to author (@mention)",
    )
    clarify_action = clarify_parser.add_mutually_exclusive_group()
    clarify_action.add_argument(
        "--list",
        dest="list_clarifications",
        action="store_true",
        help="List current clarification records",
    )
    clarify_action.add_argument(
        "--recheck",
        action="store_true",
        help="Clear the cached clarity decision so the daemon analyzes the issue again",
    )
    clarify_action.add_argument(
        "--resolve",
        action="store_true",
        help="Manually mark the clarity gate resolved and allow dispatch",
    )
    clarify_parser.add_argument(
        "--workspace",
        type=str,
        default=None,
        metavar="PATH",
        help="Explicit orchestrator workspace root",
    )
    clarify_parser.add_argument(
        "--workflow",
        type=str,
        default=None,
        metavar="PATH",
        help="Path to WORKFLOW.md (workspace discovery hint)",
    )

    # --- issue inject ---
    inject_parser = issue_sub.add_parser(
        "inject",
        help="Inject operator hints into a running agent",
        description=(
            "Send a hint to the agent. When the agent's control "
            "socket is alive, the hint is queued via pending_messages for "
            "delivery at the next tool result boundary (near-real-time). "
            "Otherwise, the hint is written to .operator_hints.md and the "
            "agent reads it at the next turn boundary. "
            "Idempotent: re-injecting the same hint is a no-op.\n\n"
            "Tips: Be concise and directive — the hint is added to the "
            "LLM's context as an operator instruction. Good examples: "
            "'Run pytest before committing', 'Check the error handling "
            "in src/api.py', 'The bug is in the date parsing logic'. "
            "The agent will see the hint in its next response but may "
            "choose how to act on it."
        ),
    )
    inject_parser.add_argument(
        "--id",
        type=str,
        required=True,
        metavar="ISSUE_ID",
        help="Issue identifier to inject hint for",
    )
    inject_parser.add_argument(
        "hint",
        nargs="?",
        default=None,
        help="Hint text to inject (omit to just list existing hints)",
    )
    inject_parser.add_argument(
        "--list",
        dest="list_hints",
        action="store_true",
        help="List existing hints for this issue",
    )
    inject_parser.add_argument(
        "--remove",
        dest="remove_hint",
        type=int,
        metavar="N",
        help="Remove hint number N",
    )
    inject_parser.add_argument(
        "--no-wait",
        dest="no_wait",
        action="store_true",
        default=False,
        help="Send inject and return immediately without waiting for delivery confirmation",
    )

    # --- issue workspace ---
    ws_parser = issue_sub.add_parser(
        "workspace",
        help="View and modify files in an issue's workspace",
        description="List, view, or edit files in an issue's workspace directory. "
        "Use with caution — concurrent edits may conflict with agent changes. "
        "Idempotent: listing and viewing are pure reads; editing overwrites.",
    )
    ws_parser.add_argument(
        "--id",
        type=str,
        required=True,
        metavar="ISSUE_ID",
        help="Issue identifier whose workspace to operate on",
    )
    ws_parser.add_argument(
        "--ls",
        action="store_true",
        help="List files in the workspace",
    )
    ws_parser.add_argument(
        "--cat",
        metavar="FILE",
        help="Show contents of a file in the workspace",
    )
    ws_parser.add_argument(
        "--edit",
        metavar="FILE",
        help="Edit a file (requires --with)",
    )
    ws_parser.add_argument(
        "--with",
        dest="content",
        metavar="CONTENT",
        help="New file content (for use with --edit)",
    )

    # --- issue review ---
    review_parser = issue_sub.add_parser(
        "review",
        help="Approve or reject a completed issue's changes (LocalTracker)",
        description="Review a LocalTracker issue after agent completes git commit. "
        "Approve to mark as completed, or reject to inject feedback and retry.",
    )
    review_parser.add_argument(
        "--id",
        type=str,
        required=True,
        metavar="ISSUE_ID",
        help="Issue identifier to review",
    )
    review_parser.add_argument(
        "--approve",
        action="store_true",
        help="Approve the changes — mark issue as completed",
    )
    review_parser.add_argument(
        "--reject",
        action="store_true",
        help="Reject the changes — inject feedback and retry",
    )
    review_parser.add_argument(
        "--feedback",
        type=str,
        default=None,
        metavar="TEXT",
        help="Feedback for rejection (required with --reject)",
    )
    review_parser.add_argument(
        "--comment",
        type=str,
        default=None,
        metavar="TEXT",
        help="Optional comment for approval",
    )

    # --- issue feedback ---
    feedback_parser = issue_sub.add_parser(
        "feedback",
        help="List, approve, or dismiss pending PR review feedback",
        description="Manage pending PR review feedback items. Use --list to show pending items, "
        "--approve to trigger follow-up for pending feedback, or --dismiss to remove "
        "feedback without processing.",
    )
    feedback_parser.add_argument(
        "--id",
        type=str,
        required=True,
        metavar="ISSUE_ID",
        help="Issue identifier with pending feedback",
    )
    feedback_parser.add_argument(
        "--list",
        action="store_true",
        dest="list_feedback",
        help="List all pending feedback items for the issue",
    )
    feedback_parser.add_argument(
        "--approve",
        action="store_true",
        help="Approve pending feedback and trigger follow-up agent run",
    )
    feedback_parser.add_argument(
        "--dismiss",
        action="store_true",
        help="Dismiss pending feedback without triggering follow-up",
    )
    feedback_parser.add_argument(
        "--feedback-id",
        type=str,
        nargs="*",
        metavar="FEEDBACK_ID",
        help="Specific feedback item IDs to approve/dismiss (all pending if omitted)",
    )
    feedback_parser.add_argument(
        "--workspace",
        type=str,
        default=None,
        metavar="PATH",
        help="Explicit workspace root path",
    )
    feedback_parser.add_argument(
        "--workflow",
        type=str,
        default=None,
        metavar="PATH",
        help="Path to WORKFLOW.md",
    )

    # --- issue diff ---
    diff_parser = issue_sub.add_parser(
        "diff",
        help="Show code changes for a completed or pending_review issue",
        description="Display a summary or full diff of changes made by the agent. "
        "Shows stats by default, use --full for complete diff output.",
    )
    diff_parser.add_argument(
        "--id",
        type=str,
        required=True,
        metavar="ISSUE_ID",
        help="Issue identifier to show diff for",
    )
    diff_parser.add_argument(
        "--full",
        action="store_true",
        help="Show complete diff output (not just summary stats)",
    )
    diff_parser.add_argument(
        "--stat",
        action="store_true",
        help="Show only file change statistics (default when no --full)",
    )
    diff_parser.add_argument(
        "--workspace",
        type=str,
        default=None,
        metavar="PATH",
        help="Explicit workspace root path (optional auto-detection override)",
    )

    # --- issue retry (CLI 兜底命令) ---
    retry_parser = issue_sub.add_parser(
        "retry",
        help="Retry/follow-up/unblock an issue via the CLI fallback",
        description="Operator-driven fallback for retry / follow-up / unblock intents when label / "
        "comment paths are inconvenient. Records the action in "
        "~/.orchestratord/orchestrator/audit.jsonl and updates the "
        "local issue registry so the next daemon poll picks up "
        "the new intent.",
    )
    retry_parser.add_argument(
        "--id",
        type=str,
        required=True,
        metavar="ISSUE_ID",
        help="Issue identifier to retry / follow-up / unblock",
    )
    retry_parser.add_argument(
        "--mode",
        type=str,
        choices=["reset", "followup", "unblock"],
        required=True,
        help="Intent mode: 'reset' clears state and re-runs (agent:retry), "
        "'followup' appends a commit to the existing branch "
        "(agent:follow-up), 'unblock' rolls an abandoned issue back "
        "to pending so the daemon reconsiders it.",
    )
    retry_parser.add_argument(
        "--reason",
        type=str,
        default="",
        metavar="TEXT",
        help="Free-form reason recorded in audit.jsonl",
    )
    retry_parser.add_argument(
        "--force",
        action="store_true",
        help="Bypass the max_retries_per_issue rate limit (CLI-only "
        "override; logged as a high-priority audit entry).",
    )
    retry_parser.add_argument(
        "--max-retries",
        type=int,
        default=3,
        metavar="N",
        help="Operator override for max_retries_per_issue (default: 3). "
        "Has no effect unless --force is also set; the audit "
        "log records both the configured limit and the actual "
        "retry_count when --force triggers a bypass.",
    )
    retry_parser.add_argument(
        "--operator",
        type=str,
        default=None,
        metavar="LOGIN",
        help="Operator login recorded in audit.jsonl (defaults to $USER / os.getlogin())",
    )
    retry_parser.add_argument(
        "--workspace",
        type=str,
        default=None,
        metavar="PATH",
        help="Explicit workspace root path (optional auto-detection override)",
    )
    retry_parser.add_argument(
        "--workflow",
        type=str,
        default=None,
        metavar="PATH",
        help="Path to WORKFLOW.md (resolution hint when metadata is missing)",
    )
    retry_parser.add_argument(
        "--stop-first",
        dest="stop_first",
        action="store_true",
        default=False,
        help="If the agent is still running, stop it first before retrying. "
        "Equivalent to 'issue stop' followed by 'issue retry'.",
    )

    # --- issue init ---
    init_parser = issue_sub.add_parser(
        "init",
        help="Scaffold an issue card from the issue-card.template.md",
        description="Copy the packaged issue-card.template.md to the specified "
        "output path and optionally replace <...> placeholders. "
        "Useful for local-tracker workflows where issues are *.md files.",
    )
    init_parser.add_argument(
        "--id",
        default="",
        metavar="ID",
        help="Issue ID (e.g. <ID>-pr-auto-fix)",
    )
    init_parser.add_argument(
        "--identifier",
        default="",
        metavar="IDENTIFIER",
        help="Short identifier (e.g. <id>)",
    )
    init_parser.add_argument(
        "--title",
        default="",
        metavar="TITLE",
        help="Issue title",
    )
    init_parser.add_argument(
        "--priority",
        default="",
        metavar="PRIORITY",
        help="Priority 0-3",
    )
    init_parser.add_argument(
        "--state",
        default="open",
        metavar="STATE",
        help="Initial state (default: open)",
    )
    init_parser.add_argument(
        "--category",
        default="",
        metavar="TAG",
        help="Category label (e.g. review-auto-fix, docs, refactor)",
    )
    init_parser.add_argument(
        "--branch-name",
        default="",
        metavar="NAME",
        help="Preferred branch name (leave blank for auto-generation)",
    )
    init_parser.add_argument(
        "--base-branch",
        default="",
        metavar="BRANCH",
        help="Base branch (e.g. dev-decoupling, main)",
    )
    init_parser.add_argument(
        "--assignee",
        default="",
        metavar="USER",
        help="Assignee / team for tracking",
    )
    init_parser.add_argument(
        "--url",
        default="",
        metavar="URL",
        help="Upstream issue / document URL",
    )
    init_parser.add_argument(
        "--output",
        "--out",
        default="./issue.md",
        metavar="FILE",
        help="Output file path (default: ./issue.md)",
    )
    init_parser.add_argument(
        "--non-interactive",
        action="store_true",
        help="Skip prompts; use defaults for missing values",
    )

    # --- issue rebase (CLI 兜底命令) ---
    rebase_parser = issue_sub.add_parser(
        "rebase",
        help="Rebase the PR's feature branch onto the latest base (CLI fallback)",
        description="Operator-driven fallback for PR conflict resolution. "
        "Writes a control file that the daemon picks up on its next "
        "poll cycle. The orchestrator itself performs the rebase "
        "(no external agent for clean rebases); the agent is only "
        "invoked if the rebase leaves actual content conflicts.",
    )
    rebase_parser.add_argument(
        "--id",
        type=str,
        required=True,
        metavar="ISSUE_ID",
        help="Issue identifier whose PR should be rebased",
    )
    rebase_parser.add_argument(
        "--force",
        action="store_true",
        help="Use plain `git push --force` (default: --force-with-lease). "
        "Bypasses the max_rebase_attempts_per_issue rate limit. "
        "Logged as a high-priority audit entry.",
    )
    rebase_parser.add_argument(
        "--reason",
        type=str,
        default="",
        metavar="TEXT",
        help="Free-form reason recorded in audit.jsonl",
    )
    rebase_parser.add_argument(
        "--operator",
        type=str,
        default=None,
        metavar="LOGIN",
        help="Operator login recorded in audit.jsonl",
    )
    rebase_parser.add_argument(
        "--workspace",
        type=str,
        default=None,
        metavar="PATH",
        help="Explicit workspace root path (auto-detection override)",
    )
    rebase_parser.add_argument(
        "--workflow",
        type=str,
        default=None,
        metavar="PATH",
        help="Path to WORKFLOW.md (resolution hint when metadata is missing)",
    )


# ---------------------------------------------------------------------------
# Run dispatch
# ---------------------------------------------------------------------------


def run(args: argparse.Namespace) -> int:
    """Dispatch to the appropriate issue subcommand."""
    cmd = args.issue_subcommand

    # Resolve workspace/registry helpers
    from orchestratord.workspace_locator import (
        get_registry_path,
        get_workspace_root,
    )

    ws = get_workspace_root(
        workspace_arg=getattr(args, "workspace", None),
        workflow_path=getattr(args, "workflow", None),
    )
    registry_path = get_registry_path(
        workspace_arg=getattr(args, "workspace", None),
        workflow_path=getattr(args, "workflow", None),
    )

    if cmd == "list":
        return _run_list(registry_path, args)
    elif cmd == "show":
        return _run_show(registry_path, args)
    elif cmd == "tail":
        return _run_tail(registry_path, args)
    elif cmd == "transcript":
        return _run_transcript(registry_path, args)
    elif cmd == "stop":
        return _run_stop(args, registry_path=registry_path, workspace_root=ws)
    elif cmd == "pause":
        return _run_pause(args, workspace_root=ws)
    elif cmd == "resume":
        return _run_resume(args, workspace_root=ws)
    elif cmd == "clarify":
        return _run_clarify(args, registry_path=registry_path, workspace_root=ws)
    elif cmd == "inject":
        return _run_inject(args)
    elif cmd == "workspace":
        return _run_workspace(args)
    elif cmd == "review":
        return _run_review(registry_path, args, workspace_root=ws)
    elif cmd == "diff":
        return _run_diff(registry_path, args)
    elif cmd == "retry":
        return _run_retry(registry_path, args, workspace_root=ws)
    elif cmd == "rebase":
        return _run_rebase(registry_path, args, workspace_root=ws)
    elif cmd == "feedback":
        return _run_feedback(registry_path, args, workspace_root=ws)
    elif cmd == "init":
        return _run_init(args)

    print(f"error: unknown issue subcommand '{cmd}'", file=sys.stderr)
    return 2


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _control_path(workspace_root: str | Path | None = None) -> Path:
    """Path to the orchestrator control directory.

    Uses the workspace root when provided (preferred), otherwise falls
    back to the ORCHESTRATORD_WORKSPACE_ROOT env var or ~/.orchestratord.
    """
    if workspace_root is not None:
        return Path(workspace_root) / ".orchestrator_control"
    base = Path(os.environ.get("ORCHESTRATORD_WORKSPACE_ROOT", Path.home() / ".orchestratord"))
    return base / ".orchestrator_control"


def _resolve_sock_path(
    issue_id: str,
    workspace_root: str | Path | None = None,
) -> Path | None:
    """Resolve the control socket path for an issue via the registry."""
    try:
        ws = Path(workspace_root) if workspace_root else None
        if ws is None:
            from orchestratord.workspace_locator import get_registry_path

            registry_path = get_registry_path()
        else:
            registry_path = ws / ".orchestratord_issue_registry.json"
        if registry_path is None or not registry_path.exists():
            return None
        from orchestratord.issue_registry import IssueRegistry

        registry = IssueRegistry(registry_path)
        record = registry.get(issue_id) or registry.get_by_identifier(issue_id)
        if record is None or not record.run_id or not record.workspace_path:
            return None
        sock_path = Path(record.workspace_path) / ".run_control" / f"{record.run_id}.sock"
        return sock_path if sock_path.exists() else None
    except Exception:
        return None


async def _send_and_wait(
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
            except asyncio.TimeoutError:
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
            pass


def _write_control(
    cmd: str, issue_id: str, extra: str = "", workspace_root: str | Path | None = None
) -> int:
    """Send a control command, preferring the Unix socket for near-real-time
    delivery. Falls back to the control-file mechanism (picked up on the
    orchestrator's next poll cycle) when the socket is unavailable.

    Socket-first delivery eliminates the 30s poll-cycle
    latency for ``pause`` / ``resume`` / ``stop`` when the agent is
    running and the control socket is alive.
    """
    from pathlib import Path

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
                from orchestratord.issue_registry import IssueRegistry

                registry = IssueRegistry(registry_path)
                record = registry.get(issue_id) or registry.get_by_identifier(issue_id)
                if record is not None and record.run_id and record.workspace_path:
                    sock_path = (
                        Path(record.workspace_path) / ".run_control" / f"{record.run_id}.sock"
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
                                    pass

                        _asyncio.run(_send_via_socket())
                        print(f"Control command '{cmd}' sent for issue {issue_id} (via socket)")
                        print(f"  The agent will process this at the next tool-result boundary.")
                        return 0
        except Exception:
            pass  # Fall through to control-file path.

    # Fallback: write a control file for the orchestrator's next poll.
    control_dir = _control_path(workspace_root=workspace_root)
    control_dir.mkdir(parents=True, exist_ok=True)

    control_file = control_dir / f"{cmd}_{issue_id}.control"
    payload = f"{cmd}\n{issue_id}\n{extra}\n"
    try:
        control_file.write_text(payload, encoding="utf-8")
        print(f"Control command '{cmd}' sent for issue {issue_id} (via control file)")
        print(f"  The orchestrator will pick this up on its next poll cycle.")
        return 0
    except Exception as exc:
        print(f"Failed to send '{cmd}' for issue {issue_id}: {exc}", file=sys.stderr)
        return 1


def _try_socket_inject(issue_id: str, hint: str) -> bool:
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

        registry_path = get_registry_path()
        if registry_path is None or not registry_path.exists():
            return False
        from orchestratord.issue_registry import IssueRegistry

        registry = IssueRegistry(registry_path)
        record = registry.get(issue_id) or registry.get_by_identifier(issue_id)
        if record is None or not record.run_id or not record.workspace_path:
            return False
        sock_path = Path(record.workspace_path) / ".run_control" / f"{record.run_id}.sock"
        if not sock_path.exists():
            return False
        import asyncio as _asyncio
        import json as _json

        async def _send() -> None:
            _reader, writer = await _asyncio.open_unix_connection(str(sock_path))
            try:
                writer.write(
                    (_json.dumps({"cmd": "inject", "payload": hint}) + "\n").encode("utf-8"),
                )
                await writer.drain()
            finally:
                writer.close()
                try:
                    await writer.wait_closed()
                except Exception:
                    pass

        _asyncio.run(_send())
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# issue list
# ---------------------------------------------------------------------------


def _run_list(registry_path: Path | None, args: argparse.Namespace) -> int:
    """List all issues with status. Idempotent — pure read."""
    if not registry_path or not registry_path.exists():
        ws = getattr(args, "workspace", None)
        wf = getattr(args, "workflow", None)

        # 当 --workspace / --workflow 都没传时，检查是否有多个活跃 orch 项目
        if not ws and not wf:
            from orchestratord.workspace_locator import (
                get_live_projects,
                print_multi_project_hint,
            )

            live = get_live_projects()
            if len(live) > 1:
                print_multi_project_hint(live, "orchestrator issue list")
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
            print(f"Orchestrator is running (PID {pid}, {p.get('project_slug', '?')})")
            print(f"Workspace: {workspace_root}")
            print("No issues processed yet.")
        else:
            print("No orchestrator registry found. No issues to list.")
            print("Hint: Start with 'orchestratord server start --workflow WORKFLOW.md'")
        return 0  # idempotent: no-issues is a valid state

    from orchestratord.issue_registry import IssueRegistry

    registry = IssueRegistry(registry_path)
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
        records = [r for r in records if _get_status_str(r.status) == status_filter]

    if not records:
        print("No issues found.")
        if status_filter:
            print(f"  (filtered by status: {status_filter})")
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

    print(f"{'ID':<20} {'STATUS':<10} {'BRANCH':<25} {'ATTEMPTS':<9} PR")
    for r in records:
        raw_status = _get_status_str(r.status)
        display_status = _STATUS_DISPLAY.get(raw_status, raw_status)
        branch = r.branch_name or "-"
        attempts = str(r.attempt_count) if r.attempt_count else "-"
        pr = r.pr_url or "-"
        print(f"{r.issue_id:<20} {display_status:<10} {branch:<25} {attempts:<9} {pr}")

    print()
    for r in records:
        s = _get_status_str(r.status)
        counts[s.upper()] = counts.get(s.upper(), 0) + 1
    print(f"  PENDING  : {counts.get('PENDING', 0)}")
    print(f"  RUNNING  : {counts.get('RUNNING', 0)}")
    print(f"  SYNCED   : {counts.get('SYNCED', 0)}")
    print(f"  COMPLETED: {counts.get('COMPLETED', 0)}")
    print(f"  FAILED   : {counts.get('FAILED', 0)}")
    print(f"  ABANDONED: {counts.get('ABANDONED', 0)}")
    return 0


# ---------------------------------------------------------------------------
# issue show
# ---------------------------------------------------------------------------


def _run_show(registry_path: Path | None, args: argparse.Namespace) -> int:
    """Show details for a specific issue. Idempotent — pure read."""
    issue_id = getattr(args, "id", None) or getattr(args, "issue_id", None)
    if not issue_id:
        print("error: --id is required", file=sys.stderr)
        return 2

    if not registry_path or not registry_path.exists():
        print(f"No registry found. Cannot show issue {issue_id}.", file=sys.stderr)
        return 1

    from orchestratord.issue_registry import IssueRegistry

    registry = IssueRegistry(registry_path)
    record = registry.get_by_issue_ref(issue_id)
    if record is None:
        print(f"Issue {issue_id} not found in registry.", file=sys.stderr)
        return 1

    import time

    created = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(record.created_at))
    updated = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(record.updated_at))

    print(f"Issue: {record.issue_id}")
    print(f"  Identifier     : {record.issue_identifier}")
    print(f"  Status         : {record.status.value}")
    print(f"  Branch         : {record.branch_name or '-'}")
    print(f"  Base Branch    : {record.base_branch or 'main'}")
    print(f"  Commit SHA     : {record.commit_sha or '-'}")
    print(f"  PR Number      : {record.pr_number or '-'}")
    print(f"  PR URL         : {record.pr_url or '-'}")
    pr_created = getattr(record, "pr_created_at", None)
    if pr_created:
        pr_created_text = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(pr_created))
        # Time from issue claim to first PR creation — the orchestrator's
        # key "issue → PR" latency metric for leadership reporting.
        latency_s = pr_created - record.created_at
        print(f"  PR Created     : {pr_created_text}")
        print(f"  Issue→PR Time  : {latency_s:.0f}s")
    else:
        print(f"  PR Created     : -")
    print(f"  Attempts       : {record.attempt_count}")
    print(f"  Run ID         : {getattr(record, 'run_id', None) or '-'}")
    print(
        f"  Turns / Tools  : {getattr(record, 'run_turn_count', 0)} / {getattr(record, 'run_tool_count', 0)}"
    )
    print(f"  Last Event     : {getattr(record, 'run_last_event', None) or '-'}")
    print(f"  Last Tool      : {getattr(record, 'run_last_tool', None) or '-'}")
    print(f"  Output Chars   : {getattr(record, 'run_output_len', 0)}")
    deadline = getattr(record, "run_timeout_deadline_at", None)
    if deadline:
        deadline_text = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(deadline))
    else:
        deadline_text = "-"
    print(f"  Timeout By     : {deadline_text}")
    workspace_dirty = getattr(record, "run_workspace_dirty", None)
    dirty_text = "-" if workspace_dirty is None else str(workspace_dirty).lower()
    print(f"  Workspace Dirty: {dirty_text}")
    print(f"  Workspace Path : {record.workspace_path or '-'}")
    print(f"  Debug Log      : {getattr(record, 'debug_log_path', None) or '-'}")
    print(f"  Created        : {created}")
    print(f"  Updated        : {updated}")
    if record.clarification_status:
        print(f"  Clarification  : {record.clarification_status}")
    _print_session_usage(record)
    return 0


def _print_session_usage(record: "IssueRecord") -> None:
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
                cost_block.get("model_usage")
                if isinstance(cost_block, dict)
                else None
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
            continue  # Skip unreadable snapshots; never fail issue show.

    if not totals["input_tokens"] and not totals["output_tokens"] and not totals["cost_usd"]:
        return
    print(
        f"  Usage (total)  : runs={len(run_ids)} input={totals['input_tokens']:.0f} "
        f"output={totals['output_tokens']:.0f} "
        f"cache_in={totals['cache_creation_input_tokens']:.0f} "
        f"cache_read={totals['cache_read_input_tokens']:.0f} "
        f"cost=${totals['cost_usd']:.4f}"
    )
    if last_detail:
        print(f"  Usage (last)   : {last_detail}")


def _resolve_issue_workspace_path(issue_id: str) -> Path | None:
    """Resolve an issue workspace, including sequential registry layouts."""
    from orchestratord.workspace_locator import get_registry_path, get_workspace_root

    workspace_root = get_workspace_root(workspace_arg=os.environ.get("ORCHESTRATORD_WORKSPACE_ROOT"))
    registry_path = get_registry_path(workspace_arg=str(workspace_root)) if workspace_root else None
    if registry_path and registry_path.exists():
        import json

        try:
            registry = json.loads(registry_path.read_text(encoding="utf-8"))
            record = registry.get(issue_id)
            if record:
                root = Path(record.get("workspace_path") or workspace_root)
                candidates = []
                identifier = record.get("issue_identifier")
                if identifier:
                    candidates.append(root / identifier)
                candidates.append(root)
                for candidate in candidates:
                    if candidate.exists():
                        return candidate
        except Exception:
            pass

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
                pass
        if wd.name == issue_id or issue_id in wd.name:
            return wd
    return None


# ---------------------------------------------------------------------------
# issue tail
# ---------------------------------------------------------------------------


def _resolve_tail_run_id(
    registry_path: Path | None,
    issue_id: str | None,
    run_id: str | None,
) -> str | None:
    """Resolve which session run to tail.

    Priority: explicit ``--run <run_id>`` wins; otherwise look up
    the most recent ``run_id`` for ``--id <issue_id>`` via the
    issue registry.  Returns ``None`` if no run can be determined.
    """
    if run_id:
        return run_id
    if not issue_id or not registry_path or not registry_path.exists():
        return None
    try:
        from orchestratord.issue_registry import IssueRegistry

        registry = IssueRegistry(registry_path)
        record = registry.get(issue_id)
        if record is None:
            record = registry.get_by_identifier(issue_id)
        if record is None:
            return None
        return record.run_id
    except Exception:
        return None


def _run_tail(registry_path: Path | None, args: argparse.Namespace) -> int:
    """Tail a session transcript for an issue or run. Idempotent — pure read.

    Unified storage: headless agent and REPL sessions both
    write to ``~/.orchestratord/sessions/{run_id}/transcript.jsonl``
    via :class:`SessionStorage`.  This command tails that file and
    renders tool calls / tool results / assistant text the same
    way the legacy ``.event_logs/{issue_id}.ndjson`` reader did.
    """
    issue_id = getattr(args, "id", None) or getattr(args, "issue_id", None)
    run_id = getattr(args, "run", None) or getattr(args, "run_id", None)
    if not issue_id and not run_id:
        print("error: --id <issue_id> or --run <run_id> is required", file=sys.stderr)
        return 2

    import time
    import json
    from pathlib import Path

    run_id = _resolve_tail_run_id(registry_path, issue_id, run_id)
    if not run_id:
        print(
            f"No session run found for issue {issue_id or '?'} (registry has no run_id recorded).",
            file=sys.stderr,
        )
        return 1

    transcript_path = SESSIONS_DIR / run_id / "transcript.jsonl"
    if not transcript_path.exists():
        print(
            f"No transcript found at {transcript_path} for run_id {run_id}.",
            file=sys.stderr,
        )
        return 1

    label = f"run {run_id}" if not issue_id else f"issue {issue_id} (run {run_id})"
    print(f"Tailing transcript for {label} (Ctrl+C to stop)...")
    try:
        last_size = transcript_path.stat().st_size
        pending = ""
        turn_counter = 0
        pending_calls: dict[str, dict] = {}
        while True:
            current_size = transcript_path.stat().st_size
            if current_size <= last_size:
                # Flush stale pending calls every 5 seconds
                import time as _time

                _time.sleep(0.5)
                continue

            with open(transcript_path, "r", encoding="utf-8") as f:
                f.seek(last_size)
                chunk = f.read()

            lines = (pending + chunk).splitlines(keepends=True)
            if lines and not lines[-1].endswith("\n"):
                pending = lines.pop()
            else:
                pending = ""

            for line in lines:
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError as exc:
                    print(
                        f"[tail] warning: malformed entry in {transcript_path}: {exc}",
                        file=sys.stderr,
                    )
                    continue
                _render_message(msg, turn_counter, pending_calls)
            last_size = current_size
    except KeyboardInterrupt:
        print("\n[tail] stopped")
    except Exception as exc:
        print(f"[tail] error: {exc}", file=sys.stderr)
        return 1
    return 0


def _format_ts(timestamp_str: str | None) -> str:
    """Format an ISO-8601 timestamp string to ``HH:MM:SS``.

    Falls back to the current local time when the transcript entry
    has no timestamp (legacy records, session_snapshot lines, etc.).
    """
    if timestamp_str:
        try:
            from datetime import datetime

            dt = datetime.fromisoformat(timestamp_str)
            return dt.strftime("%H:%M:%S")
        except (ValueError, TypeError):
            pass
    from datetime import datetime

    return datetime.now().strftime("%H:%M:%S")


def _summarize_tool_args(name: str, inp: dict) -> str:
    """Return a one-line argument summary for a tool call.

    Examples::

        Read → ``src/services/lock.py``
        Grep → ``"asyncio.Lock"``
        Edit → ``src/services/lock.py``
        Bash → ``pytest tests/test_lock.py``
        Git  → ``commit -m "fix: …"``
    """
    if not inp:
        return ""
    if name == "Read":
        return inp.get("file_path", inp.get("path", "")).strip()
    if name == "Grep" or name == "grep":
        pat = inp.get("pattern", "")
        return f'"{pat}"' if pat else ""
    if name in ("Edit", "Write", "create", "Create"):
        return inp.get("file_path", inp.get("path", "")).strip()
    if name == "Bash" or name == "bash" or name == "Git" or name == "git":
        cmd = inp.get("command", "")
        return cmd.strip()[:90]
    # Fallback: join first 3 non-empty string values
    parts = [str(v)[:60] for v in inp.values() if isinstance(v, str) and v.strip()]
    return " ".join(parts[:3])


def _summarize_tool_result(name: str, content: str | list | None) -> str:
    """Return a brief one-line result summary for a tool result.

    Returns empty string when no meaningful summary can be inferred.
    """
    if not content:
        return ""
    text = ""
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text", "")
                break

    if not text.strip():
        return ""

    # Line count for Read
    if name in ("Read", "read"):
        n = text.count("\n") + 1
        return f"{n} lines"

    # Hit count for Grep
    if name in ("Grep", "grep"):
        lines = text.strip().splitlines()
        # Count actual match lines (omit "X results" footer / header lines)
        match_lines = [l for l in lines if l.strip() and not l.startswith("─")]
        return f"{len(match_lines)} hits" if match_lines else "0 hits"

    # Diff stat for Edit
    if name in ("Edit", "edit", "Write", "write"):
        added = text.count("+")  # rough heuristic
        removed = text.count("-")  # rough heuristic
        n = text.count("\n") + 1 if text.strip() else 0
        # If the result is just "No changes" or similar, say so
        text_lower = text.strip().lower()
        if "no change" in text_lower or "nothing" in text_lower:
            return "no changes"
        # Show first line of diff patch as preview
        first_line = text.strip().splitlines()[0] if text.strip() else ""
        if first_line.startswith("diff --git"):
            parts = text.strip().splitlines()
            # Try to find a hunk header like @@ -1,3 +1,6 @@
            hunk = ""
            for p in parts:
                if p.startswith("@@"):
                    hunk = p
                    break
            return f"+{n} lines" if n > 0 else "0 changes"
        return f"+{n} lines" if n > 0 else ""

    # Exit code / summary for Bash
    if name in ("Bash", "bash"):
        first = text.strip().splitlines()[0] if text.strip() else ""
        # Look for common test result patterns
        passed = ""
        import re

        m = re.search(r"(\d+)\s+passed", text)
        if m:
            passed = m.group(0)
        failed = ""
        m = re.search(r"(\d+)\s+failed", text)
        if m:
            failed = m.group(0)
        if passed or failed:
            parts = [p for p in (passed, failed) if p]
            return " · ".join(parts) if parts else "done"
        # Return first meaningful output line
        first = first.rstrip("\n")[:60]
        return first if first else "done"

    return ""


def _render_message(msg: dict, turn_counter: int, pending_calls: dict) -> None:
    """Render one Message dict from transcript.jsonl as a tail line.

    Produces output matching the README Demo format::

        14:02:11  ◐ Read src/services/lock.py · 132 lines
        14:02:13  ◐ Grep "asyncio.Lock" · 3 hits
        14:02:18  ◐ Edit src/services/lock.py · +18 -4
        14:02:24  ◐ Bash pytest tests/test_lock.py · 4 passed
        14:02:24  ✓ Verification gate OK (pytest -x)
        14:02:25  ◐ Git commit -m "fix: per-key lock granularity in flush_batch"
        14:02:26  ◐ Git push origin orchestratord/AGENTSDK-15
        14:02:31  ✓ PR opened · auto-review-loop subscribed

    tool_use + tool_result pairs are merged into a single line by
    buffering the tool_use in ``pending_calls`` (keyed by tool_use_id)
    and rendering when the matching tool_result arrives.
    """
    role = msg.get("role", "?")
    content = msg.get("content")
    if not isinstance(content, list):
        return

    ts = _format_ts(msg.get("timestamp"))

    for block in content:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")

        # -- tool_use: buffer the call, render when result arrives --
        if btype == "tool_use" and role == "assistant":
            name = block.get("name", "?")
            tid = block.get("id") or block.get("tool_use_id", "")
            inp = block.get("input", {})
            pending_calls[tid] = {
                "name": name,
                "input": inp,
                "timestamp": ts,
            }

        # -- tool_result: pair with buffered tool_use and render --
        elif btype == "tool_result" and role == "user":
            tid = block.get("tool_use_id", "?")
            err = block.get("is_error", False)
            result_content = block.get("content", "")

            call = pending_calls.pop(tid, None)
            if call:
                name = call["name"]
                inp = call["input"]
                call_ts = call["timestamp"]
                args_str = _summarize_tool_args(name, inp)
                result_str = _summarize_tool_result(name, result_content)

                icon = "✗" if err else "◐"
                line = f"{call_ts}  {icon} {name}"
                if args_str:
                    line += f" {args_str}"
                if result_str:
                    line += f" · {result_str}"
                print(line)
            else:
                icon = "✗" if err else "◐"
                print(f"{ts}  {icon} [result {tid}]")

        # -- assistant text: special-cased detection --
        elif btype == "text" and role == "assistant":
            text = (block.get("text") or "").strip()
            if not text:
                continue

            lower = text.lower()

            # Verification gate passed
            if "pytest" in lower and ("passed" in lower or "ok" in lower):
                preview = text[:80].replace("\n", " ")
                # Strip to a single line
                preview = preview.strip()
                print(f"{ts}  ✓ Verification gate OK ({preview})")
            # PR opened
            elif "pr opened" in lower or "pull request" in lower or "opened pr" in lower:
                preview = text[:80].replace("\n", " ")
                print(f"{ts}  ✓ PR opened · {preview.strip()}")
            # Git operations
            elif lower.startswith("git") or "git commit" in lower or "committed" in lower:
                preview = text[:80].replace("\n", " ")
                print(f"{ts}  ◐ {preview.strip()}")
            elif "push" in lower and ("git" in lower or "origin" in lower):
                preview = text[:80].replace("\n", " ")
                print(f"{ts}  ◐ {preview.strip()}")
            # Generic assistant text
            else:
                preview = text[:80].replace("\n", " ")
                print(f"{ts}  ◐ {preview.strip()}")


def _msg_references_tool(msg: dict, tool_use_id: str) -> bool:
    """Whether a Message dict contains any block referring to tool_use_id.

    Matches both ``tool_use.id`` and ``tool_result.tool_use_id`` so the
    filter surfaces the full tool_use + tool_result pair, not just one
    half of it.
    """
    content = msg.get("content")
    if not isinstance(content, list):
        return False
    for block in content:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "tool_use" and block.get("id") == tool_use_id:
            return True
        if btype == "tool_result" and block.get("tool_use_id") == tool_use_id:
            return True
    return False


def _print_message(
    msg: dict,
    tool_use_id_filter: str | None = None,
) -> None:
    """Print one Message dict from transcript.jsonl in human-readable form.

    Designed for `issue transcript` (snapshot mode) — full content
    instead of the one-line preview used by `issue tail`.

    When ``tool_use_id_filter`` is set, only blocks that reference that
    tool_use id are printed: ``tool_use.id == filter`` or
    ``tool_result.tool_use_id == filter``. Text blocks in the same
    message are suppressed under filter, so a single multi-tool
    assistant message prints only the relevant tool_use (not the
    unrelated ones that share the same message).
    """
    role = msg.get("role", "?")
    origin = msg.get("origin", "")
    origin_suffix = f" (origin={origin})" if origin else ""
    print(f"## {role}{origin_suffix}")
    content = msg.get("content")
    if isinstance(content, str):
        if tool_use_id_filter is None:
            for line in content.splitlines():
                print(f"  Text: {line}")
        print()
        return
    if not isinstance(content, list):
        return
    printed_any_block = False
    for block in content:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if tool_use_id_filter is not None:
            if btype == "tool_use" and block.get("id") != tool_use_id_filter:
                continue
            if btype == "tool_result" and block.get("tool_use_id") != tool_use_id_filter:
                continue
            if btype == "text":
                continue
        if btype == "text":
            text = (block.get("text") or "").rstrip()
            if text:
                for line in text.splitlines():
                    print(f"  Text: {line}")
                printed_any_block = True
        elif btype == "tool_use":
            tid = block.get("id", "?")
            name = block.get("name", "?")
            print(f"  Tool Use: {name} (id={tid})")
            inp = block.get("input", {})
            if isinstance(inp, dict):
                for k, v in inp.items():
                    preview = str(v).replace("\n", " ")
                    if len(preview) > 200:
                        preview = preview[:200] + "..."
                    print(f"    {k}: {preview}")
            printed_any_block = True
        elif btype == "tool_result":
            tid = block.get("tool_use_id", "?")
            err = " [ERROR]" if block.get("is_error") else ""
            print(f"  Tool Result: {tid}{err}")
            result_content = block.get("content", "")
            if isinstance(result_content, str):
                lines = result_content.splitlines()
                for line in lines[:50]:
                    print(f"    {line}")
                if len(lines) > 50:
                    print(
                        f"    ... ({len(lines) - 50} more lines)",
                    )
            else:
                print(f"    {result_content!r}")
            printed_any_block = True
    if tool_use_id_filter is not None and not printed_any_block:
        # Header was already printed; emit a blank line for visual
        # separation but otherwise stay quiet (the matching blocks
        # live in another message that will be printed separately).
        pass
    print()


def _run_transcript(registry_path: Path | None, args: argparse.Namespace) -> int:
    """Print the full session transcript for an issue or run. Idempotent.

    Read-only access to the unified
    ``~/.orchestratord/sessions/{run_id}/transcript.jsonl`` so operators
    can review a completed (or in-progress) orchestrator run without
    entering an interactive REPL.  Suitable for piping.
    """
    issue_id = getattr(args, "id", None)
    run_id = getattr(args, "run", None) or getattr(args, "run_id", None)
    if not issue_id and not run_id:
        print(
            "error: --id <issue_id> or --run <run_id> is required",
            file=sys.stderr,
        )
        return 2

    import json
    run_id = _resolve_tail_run_id(registry_path, issue_id, run_id)
    if not run_id:
        print(
            f"No session run found for issue {issue_id or '?'} (registry has no run_id recorded).",
            file=sys.stderr,
        )
        return 1

    transcript_path = SESSIONS_DIR / run_id / "transcript.jsonl"
    if not transcript_path.exists():
        print(
            f"No transcript found at {transcript_path} for run_id {run_id}.",
            file=sys.stderr,
        )
        return 1

    role_filter = getattr(args, "role", None)
    tool_use_id_filter = getattr(args, "tool_use_id", None)
    limit = getattr(args, "limit", None)

    print(f"# Transcript for run {run_id}")
    if issue_id:
        print(f"# (issue {issue_id})")
    print(f"# Source: {transcript_path}")
    print()

    count = 0
    with open(transcript_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError as exc:
                print(
                    f"[transcript] warning: malformed entry: {exc}",
                    file=sys.stderr,
                )
                continue

            if role_filter and msg.get("role") != role_filter:
                continue

            if tool_use_id_filter and not _msg_references_tool(
                msg,
                tool_use_id_filter,
            ):
                continue

            _print_message(msg, tool_use_id_filter=tool_use_id_filter)
            count += 1
            if limit is not None and count >= limit:
                break

    print(f"# {count} message(s) shown")
    return 0


# ---------------------------------------------------------------------------
# issue stop
# ---------------------------------------------------------------------------


def _run_stop(
    args: argparse.Namespace,
    registry_path: Path | None = None,
    workspace_root: str | Path | None = None,
) -> int:
    """Stop a running issue agent. Idempotent — already-stopped → success."""
    issue_id = getattr(args, "id", None)
    if not issue_id:
        print("error: --id is required", file=sys.stderr)
        return 2

    skip_confirm = getattr(args, "yes", False)

    # Check registry for current status (best-effort)
    current_status = None
    if registry_path and registry_path.exists():
        try:
            from orchestratord.issue_registry import IssueRegistry, IssueStatus

            registry = IssueRegistry(registry_path)
            record = registry.get(issue_id) or registry.get_by_identifier(issue_id)
            if record is not None:
                current_status = record.status.value
        except Exception as exc:
            print(f"Warning: could not read registry: {exc}", file=sys.stderr)

    if current_status is not None:
        # RUNNING is the only status where the stop command will be effective.
        # For all other statuses the orchestrator cannot find the issue in
        # _state.running and the control file will be silently ignored.
        if current_status != "running":
            print(
                f"Warning: issue {issue_id} is not currently running (status: {current_status}).",
                file=sys.stderr,
            )
            print(
                "  The stop command will not take effect — no agent session to stop.",
                file=sys.stderr,
            )
            if not skip_confirm:
                try:
                    raw = input("  Write control file anyway? [y/N]: ")
                    if raw.strip().lower() not in ("y", "yes"):
                        print("Stop cancelled.")
                        return 0
                except (EOFError, KeyboardInterrupt):
                    print("\nStop cancelled.")
                    return 0
            else:
                print("  (use --id to target a running issue)")
    else:
        print(
            f"Warning: issue {issue_id} not found in registry — cannot verify current status.",
            file=sys.stderr,
        )
        if not skip_confirm:
            try:
                raw = input("  Write stop control file anyway? [y/N]: ")
                if raw.strip().lower() not in ("y", "yes"):
                    print("Stop cancelled.")
                    return 0
            except (EOFError, KeyboardInterrupt):
                print("\nStop cancelled.")
                return 0

    # Confirmation prompt (unless --yes is set)
    if not skip_confirm:
        try:
            raw = input(f"Stop agent for issue {issue_id}? [y/N]: ")
            if raw.strip().lower() not in ("y", "yes"):
                print("Stop cancelled.")
                return 0
        except (EOFError, KeyboardInterrupt):
            print("\nStop cancelled.")
            return 0

    print(f"Issue stop: sending stop command for {issue_id}")
    no_wait = getattr(args, "no_wait", False)

    sock_path = _resolve_sock_path(issue_id, workspace_root)
    if sock_path is not None and not no_wait:

        async def _do_stop() -> int:
            t0 = asyncio.get_event_loop().time()
            data = await _send_and_wait(sock_path, "stop", "", "SessionComplete", timeout=10.0)
            elapsed = asyncio.get_event_loop().time() - t0
            if data is not None:
                print(f"Agent stopped ({elapsed:.1f}s).")
                return 0
            else:
                print(
                    f"Stop sent. Agent is unwinding "
                    f"(may take a few seconds for long-running tools)."
                )
                return 0

        return asyncio.run(_do_stop())
    else:
        return _write_control("stop", issue_id, workspace_root=workspace_root)


# ---------------------------------------------------------------------------
# issue pause
# ---------------------------------------------------------------------------


def _run_pause(args: argparse.Namespace, workspace_root: str | Path | None = None) -> int:
    """Pause a running issue agent. Idempotent — already-paused → success."""
    issue_id = getattr(args, "id", None)
    if not issue_id:
        print("error: --id is required", file=sys.stderr)
        return 2
    reason = getattr(args, "reason", "") or "operator requested pause"
    no_wait = getattr(args, "no_wait", False)

    sock_path = _resolve_sock_path(issue_id, workspace_root)
    if sock_path is not None and not no_wait:
        started = asyncio.get_event_loop().time() if asyncio.get_event_loop().is_running() else None

        async def _do_pause() -> int:
            t0 = asyncio.get_event_loop().time()
            data = await _send_and_wait(sock_path, "pause", reason, "Paused", timeout=30.0)
            elapsed = asyncio.get_event_loop().time() - t0
            if data is not None:
                turn = data.get("turn", "?")
                tool = data.get("tool_name", "?")
                print(f"Agent paused at turn {turn}, tool {tool!r} ({elapsed:.1f}s).")
                return 0
            else:
                print(
                    f"Pause acknowledged but agent is in a long operation "
                    f"(30s timeout). It will pause at the next tool boundary."
                )
                return 0

        return asyncio.run(_do_pause())
    elif sock_path is not None and no_wait:
        # Fire and forget via socket.
        return _write_control("pause", issue_id, reason, workspace_root=workspace_root)
    else:
        print(f"Issue pause: sending pause command for {issue_id}")
        return _write_control("pause", issue_id, reason, workspace_root=workspace_root)


# ---------------------------------------------------------------------------
# issue resume
# ---------------------------------------------------------------------------


def _run_resume(args: argparse.Namespace, workspace_root: str | Path | None = None) -> int:
    """Resume a paused issue agent. Idempotent — running → success."""
    issue_id = getattr(args, "id", None)
    if not issue_id:
        print("error: --id is required", file=sys.stderr)
        return 2
    no_wait = getattr(args, "no_wait", False)

    sock_path = _resolve_sock_path(issue_id, workspace_root)
    if sock_path is not None and not no_wait:

        async def _do_resume() -> int:
            t0 = asyncio.get_event_loop().time()
            data = await _send_and_wait(sock_path, "resume", "", "Resumed", timeout=5.0)
            elapsed = asyncio.get_event_loop().time() - t0
            if data is not None:
                print(f"Agent resumed ({elapsed:.1f}s).")
                return 0
            else:
                print(f"Resume sent but no confirmation (5s). The agent may already be running.")
                return 0

        return asyncio.run(_do_resume())
    elif sock_path is not None and no_wait:
        return _write_control("resume", issue_id, workspace_root=workspace_root)
    else:
        print(f"Issue resume: sending resume command for {issue_id}")
        return _write_control("resume", issue_id, workspace_root=workspace_root)


# ---------------------------------------------------------------------------
# issue clarify
# ---------------------------------------------------------------------------


def _run_clarify(
    args: argparse.Namespace,
    *,
    registry_path: Path | None = None,
    workspace_root: Path | None = None,
) -> int:
    """Answer a clarification request. Idempotent — re-answering updates in place."""
    issue_id = getattr(args, "id", None)
    list_clarifications = bool(getattr(args, "list_clarifications", False))
    if not issue_id and not list_clarifications:
        print("error: --id is required", file=sys.stderr)
        return 2

    answer = getattr(args, "answer", None)
    forward = getattr(args, "forward_to_author", False)

    recheck = bool(getattr(args, "recheck", False))
    resolve = bool(getattr(args, "resolve", False))
    if not answer and not forward and not list_clarifications and not recheck and not resolve:
        print("error: --answer is required unless --forward-to-author is used", file=sys.stderr)
        return 2

    from orchestratord.issue_clarifier.queue import ClarificationQueue
    from orchestratord.issue_registry import IssueRegistry

    queue_path = (
        Path(workspace_root) / ".orchestratord_clarification_queue.json"
        if workspace_root is not None
        else None
    )
    queue = ClarificationQueue(queue_path)

    if list_clarifications:
        items = queue.list_items()
        if not items:
            print("No clarification records.")
            return 0
        for item in items:
            print(f"{item.issue_id}\t{item.status.value}\t{item.question}")
        return 0

    registry = IssueRegistry(registry_path) if registry_path is not None else None
    if recheck:
        queue.remove(issue_id)
        record = registry.get(issue_id) if registry is not None else None
        if record is None:
            print(f"Issue {issue_id} is not present in the registry.", file=sys.stderr)
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
        print(f"Issue {issue_id} will be rechecked on the next poll cycle.")
        return 0

    if resolve:
        queue.remove(issue_id)
        if registry is None:
            print("Could not locate the issue registry.", file=sys.stderr)
            return 1
        record = registry.get(issue_id)
        if record is None:
            print(f"Issue {issue_id} is not present in the registry.", file=sys.stderr)
            return 1
        registry.mark_clarification_resolved(
            issue_id,
            fingerprint=record.clarifier_fingerprint or "manual",
            answer=answer or "Manually resolved by operator",
            source="operator",
            status="manual_resolved",
        )
        print(f"Issue {issue_id} clarification marked resolved.")
        return 0

    if forward:
        item = queue.mark_awaiting_author(issue_id)
        if item is None:
            print(f"No pending clarification for issue {issue_id}.", file=sys.stderr)
            return 1
        print(f"Issue {issue_id} marked for author clarification.")
        return 0

    resolved = queue.resolve(issue_id, answer or "", source="clarification_queue")
    if resolved is None:
        print(f"Failed to write answer for issue {issue_id}.", file=sys.stderr)
        return 1

    print(f"Answer recorded for issue {issue_id}: {answer or '(forwarded to author)'}")
    print(f"Status: {resolved.status.value}")
    print(f"The orchestrator will pick this up on its next poll cycle.")
    return 0


# ---------------------------------------------------------------------------
# issue inject
# ---------------------------------------------------------------------------


def _run_inject(args: argparse.Namespace) -> int:
    """Inject operator hints. Idempotent — listing/removal are safe."""
    issue_id = getattr(args, "id", None)
    if not issue_id:
        print("error: --id is required", file=sys.stderr)
        return 2

    ws_path = _resolve_issue_workspace_path(issue_id)
    hints_file = ws_path / ".operator_hints.md" if ws_path else None
    if hints_file is None:
        print(
            f"Could not find workspace for issue {issue_id}.\n"
            "Hints are stored in the issue's workspace directory.\n"
            "Set ORCHESTRATORD_WORKSPACE_ROOT or run the orchestrator with --workflow.",
            file=sys.stderr,
        )
        return 1

    hint = getattr(args, "hint", None)
    list_hints = getattr(args, "list_hints", False)
    remove_hint = getattr(args, "remove_hint", None)

    if list_hints or (not hint and remove_hint is None):
        # List hints
        return _list_hints(issue_id, hints_file)
    elif remove_hint is not None:
        return _remove_hint(issue_id, hints_file, remove_hint)
    elif hint:
        no_wait = getattr(args, "no_wait", False)
        sock_path = _resolve_sock_path(issue_id)
        if sock_path is not None and not no_wait:

            async def _do_inject() -> int:
                t0 = asyncio.get_event_loop().time()

                # 1. Pause the agent so the message can be safely
                #    written to the transcript at a clean boundary.
                try:
                    pause_data = await _send_and_wait(
                        sock_path,
                        "pause",
                        "",
                        "Paused",
                        timeout=30.0,
                    )
                    if pause_data is None:
                        print(
                            "warning: pause not confirmed within 30s — "
                            "agent may be in a long operation or already "
                            "paused. Injecting anyway.",
                            file=sys.stderr,
                        )
                except (ConnectionRefusedError, FileNotFoundError, OSError):
                    # Socket gone — fall back to file.
                    return _inject_hint(issue_id, hints_file, hint)

                # 2. Inject the message (writes UserMessage to transcript
                #    + queues for in-memory Conversation).
                try:
                    data = await _send_and_wait(
                        sock_path,
                        "inject",
                        hint,
                        "InjectDelivered",
                        timeout=30.0,
                    )
                except (ConnectionRefusedError, FileNotFoundError, OSError):
                    return _inject_hint(issue_id, hints_file, hint)

                # 3. Auto-resume so the agent processes the message.
                try:
                    await _send_and_wait(
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
                    print(
                        f"Message injected and agent resumed ({elapsed:.1f}s). "
                        f"Agent will see it in its next response."
                    )
                    if snippet:
                        print(f"  hint: {snippet}{'...' if len(hint) > 80 else ''}")
                    return 0
                else:
                    print(
                        f"Hint queued ({elapsed:.1f}s). "
                        f"Will be delivered at next tool result boundary."
                    )
                    return 0

            return asyncio.run(_do_inject())
        elif sock_path is not None and no_wait:
            if _try_socket_inject(issue_id, hint):
                print(
                    f"\u2713 hint injected for issue {issue_id}"
                    f" \u00b7 agent will receive it at the next tool result boundary"
                )
                return 0
            return _inject_hint(issue_id, hints_file, hint)
        else:
            return _inject_hint(issue_id, hints_file, hint)
    else:
        return _list_hints(issue_id, hints_file)


def _parse_hints_file(hints_file: Path) -> list[tuple[float, str]]:
    """Parse hints file into list of (timestamp, hint) tuples."""
    import time

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
                    ts_str = parts[1].rstrip(") ---")
                    from datetime import datetime

                    dt = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S")
                    ts = dt.timestamp()
                else:
                    ts = time.time()
            except Exception:
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


def _inject_hint(issue_id: str, hints_file: Path, hint: str) -> int:
    """Append a hint to the .operator_hints.md file.

    Idempotent: if the hint text already exists in the file, it is
    not duplicated.
    """
    import time

    hints = _parse_hints_file(hints_file)
    # Idempotency — skip if the exact hint text
    # already exists.
    for _ts, existing_hint in hints:
        if existing_hint.strip() == hint.strip():
            print(f"Hint already exists for issue {issue_id} — no action taken.")
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
        print(
            f"\u2713 hint injected for issue {issue_id}"
            f" \u00b7 agent will pick it up at the next tool result boundary"
        )
        return 0
    except Exception as exc:
        print(f"Failed to inject hint: {exc}", file=sys.stderr)
        return 1


def _list_hints(issue_id: str, hints_file: Path) -> int:
    """List all hints for an issue."""
    hints = _parse_hints_file(hints_file)
    if not hints:
        print(f"No hints for issue {issue_id}.")
        return 0
    print(f"Hints for issue {issue_id}:")
    for i, (ts, hint) in enumerate(hints, 1):
        import time

        ts_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))
        preview = hint[:60].replace("\n", " ")
        print(f"  #{i}: [{ts_str}] {preview}")
    return 0


def _remove_hint(issue_id: str, hints_file: Path, hint_num: int) -> int:
    """Remove a hint by number."""
    hints = _parse_hints_file(hints_file)
    if hint_num < 1 or hint_num > len(hints):
        print(f"Hint #{hint_num} not found (have {len(hints)} hints).", file=sys.stderr)
        return 1

    hints.pop(hint_num - 1)
    # Rebuild file
    import time

    content = ""
    for i, (ts, hint) in enumerate(hints, 1):
        ts_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))
        header = f"--- Operator Hint #{i} (injected at {ts_str}) ---\n"
        separator = "-" * 50 + "\n"
        content += header + hint + "\n" + separator
    hints_file.write_text(content, encoding="utf-8")
    print(f"Removed hint #{hint_num} for issue {issue_id}.")
    return 0


# ---------------------------------------------------------------------------
# issue workspace
# ---------------------------------------------------------------------------


def _run_workspace(args: argparse.Namespace) -> int:
    """View or modify workspace files. Workspace listing/view are pure reads."""
    issue_id = getattr(args, "id", None)
    if not issue_id:
        print("error: --id is required", file=sys.stderr)
        return 2

    ws_path = _resolve_issue_workspace_path(issue_id)
    if ws_path is None:
        print(f"Could not find workspace for issue {issue_id}.", file=sys.stderr)
        return 1

    ls_flag = getattr(args, "ls", False)
    cat_flag = getattr(args, "cat", None)
    edit_flag = getattr(args, "edit", None)
    content = getattr(args, "content", None)

    if ls_flag:
        return _workspace_list_files(issue_id, ws_path)
    elif cat_flag:
        return _workspace_cat_file(issue_id, ws_path, cat_flag)
    elif edit_flag:
        if not content:
            print("error: --edit requires --with <content>", file=sys.stderr)
            return 2
        return _workspace_edit_file(issue_id, ws_path, edit_flag, content)
    else:
        return _workspace_list_files(issue_id, ws_path)


def _workspace_list_files(issue_id: str, ws_path: Path) -> int:
    """List files in workspace. Idempotent — pure read."""
    if not ws_path.exists():
        print(f"Workspace for issue {issue_id} not found.", file=sys.stderr)
        return 1

    exclude = {".metadata", ".orchestrator_control", ".operator_hints.md"}
    print(f"Workspace for issue {issue_id}: {ws_path}")
    print("-" * 60)

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
        print(f"  [DIR]  {d}")
    for f in files:
        print(f"  {f}")
    if not files and not dirs:
        print("  (empty workspace)")
    return 0


def _workspace_cat_file(issue_id: str, ws_path: Path, filename: str) -> int:
    """Show file contents. Idempotent — pure read."""
    file_path = ws_path / filename
    if not file_path.exists():
        print(f"File not found: {filename}", file=sys.stderr)
        return 1
    if not file_path.is_file():
        print(f"Not a file: {filename}", file=sys.stderr)
        return 1
    try:
        content = file_path.read_text(encoding="utf-8")
        print(f"=== {filename} ===")
        print(content)
    except Exception as exc:
        print(f"Failed to read {filename}: {exc}", file=sys.stderr)
        return 1
    return 0


def _workspace_edit_file(issue_id: str, ws_path: Path, filename: str, content: str) -> int:
    """Write new content to a file."""
    file_path = ws_path / filename
    file_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        file_path.write_text(content, encoding="utf-8")
        print(f"Updated {filename} in issue {issue_id} workspace.")
        print(f"  The agent will see this change on its next tool call.")
        return 0
    except Exception as exc:
        print(f"Failed to write {filename}: {exc}", file=sys.stderr)
        return 1


# ---------------------------------------------------------------------------
# issue review
# ---------------------------------------------------------------------------


def _tracker_from_workflow_arg(args: argparse.Namespace) -> Any | None:
    workflow_path = getattr(args, "workflow", None)
    if not workflow_path:
        return None
    try:
        from orchestratord.tracker import create_tracker_adapter
        from orchestratord.workflow import WorkflowLoader

        workflow, _ = WorkflowLoader.load(workflow_path)
        return create_tracker_adapter(workflow.tracker)
    except Exception as exc:
        print(f"Warning: could not initialize tracker from workflow: {exc}", file=sys.stderr)
        return None


def _mirror_intent_label(
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
        import asyncio

        async def call() -> bool:
            return bool(await method(issue_id, label))

        return asyncio.run(call())
    except Exception as exc:  # noqa: BLE001
        verb = "remove" if remove else "add"
        print(
            f"Warning: could not {verb} {label} label on issue {issue_id}: {exc}",
            file=sys.stderr,
        )
        return False


def _run_review(
    registry_path: Path | None, args: argparse.Namespace, workspace_root: str | Path | None = None
) -> int:
    """Approve or reject a LocalTracker issue's changes."""
    issue_id = getattr(args, "id", None)
    if not issue_id:
        print("error: --id is required", file=sys.stderr)
        return 2

    if not registry_path or not registry_path.exists():
        print(f"No registry found. Cannot review issue {issue_id}.", file=sys.stderr)
        return 1

    from orchestratord.issue_registry import IssueRegistry, IssueStatus

    registry = IssueRegistry(registry_path)
    record = registry.get(issue_id)
    if record is None:
        print(f"Issue {issue_id} not found in registry.", file=sys.stderr)
        return 1

    approve = getattr(args, "approve", False)
    reject = getattr(args, "reject", False)

    if not approve and not reject:
        print("error: specify --approve or --reject", file=sys.stderr)
        return 2

    recoverable_failed_completion = bool(
        record.status is IssueStatus.COMPLETED
        and (
            record.verification_status == "failed"
            or record.last_hook_error
            or record.session_end_reason == "empty_branch_no_commits"
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
        print(
            f"Issue {issue_id} is not pending review (status: {record.status.value}).",
            file=sys.stderr,
        )
        print(
            "Only issues with 'pending_review' status, or an already queued "
            "rejection retry, can be reviewed.",
            file=sys.stderr,
        )
        return 1

    if reject:
        feedback = getattr(args, "feedback", None)
        if not feedback:
            print("error: --reject requires --feedback", file=sys.stderr)
            return 2

        # The daemon owns its in-memory registry, lifecycle sets, tracker state,
        # and clarification queue. Send one durable control command so those
        # related mutations happen together on the next poll instead of
        # partially updating the same files through stale CLI-side objects.
        rc = _write_control("review_retry", issue_id, feedback, workspace_root=workspace_root)
        if rc != 0:
            return rc

        print(f"Issue {issue_id} rejected with feedback:")
        print(f'  "{feedback}"')
        print(f"Feedback queued — orchestrator will retry this issue.")
        return 0

    if approve:
        comment = getattr(args, "comment", None)
        rc = _write_control(
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
        print(f"Issue {issue_id} approval queued — orchestrator will finalize it.")
        return 0


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------
# issue feedback
# ---------------------------------------------------------------------------

# Matches a repo web URL like
#   https://gitcode.com/Gideon_Zhao/perf-reference-ascend/merge_requests/3
#   https://gitee.com/acme/widget/pulls/9
#   https://github.com/acme/widget/pull/12
# capturing host / owner / repo so we can rebuild a comment permalink.
_PR_URL_RE = re.compile(r"^(?P<host>https?://[^/]+)/(?P<owner>[^/]+)/(?P<repo>[^/]+)/")


def _fallback_feedback_url(record: Any, feedback_id: str) -> str | None:
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
    if raw_issue_id.startswith("#"):
        raw_issue_id = raw_issue_id[1:]
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


def _run_feedback(
    registry_path: Path | None, args: argparse.Namespace, workspace_root: str | Path | None = None
) -> int:
    """List, approve, or dismiss pending PR review feedback."""
    issue_id = getattr(args, "id", None)
    if not issue_id:
        print("error: --id is required", file=sys.stderr)
        return 2

    if not registry_path or not registry_path.exists():
        print(f"No registry found. Cannot manage feedback for issue {issue_id}.", file=sys.stderr)
        return 1

    from orchestratord.issue_registry import IssueRegistry

    registry = IssueRegistry(registry_path)
    record = registry.get(issue_id)
    if record is None:
        print(f"Issue {issue_id} not found in registry.", file=sys.stderr)
        return 1

    list_feedback = getattr(args, "list_feedback", False)
    approve = getattr(args, "approve", False)
    dismiss = getattr(args, "dismiss", False)

    if not list_feedback and not approve and not dismiss:
        print("error: specify --list, --approve, or --dismiss", file=sys.stderr)
        return 2

    if list_feedback:
        if not record.pending_feedback_ids:
            print(f"No pending feedback for issue {issue_id}.")
            return 0
        print(f"Pending feedback for issue {issue_id}:")
        print("(use the ID with --feedback-id to approve/dismiss a single item)")
        for i, fid in enumerate(record.pending_feedback_ids, 1):
            # Resolve the canonical comment/check URL when available
            # (persisted from the tracker's html_url). Fall back to
            # reconstructing it from pr_url + raw comment id (GitCode's
            # issue-comments API omits html_url). No URL for review_summary
            # / ci sources -> show the id alone.
            url = record.pending_feedback_urls.get(fid) or _fallback_feedback_url(record, fid)
            if url:
                print(f"  {i}. {fid}  ->  {url}")
            else:
                print(f"  {i}. {fid}")
        print(f"\nTotal: {len(record.pending_feedback_ids)} pending item(s)")
        return 0

    target_ids = getattr(args, "feedback_id", None) or list(record.pending_feedback_ids)
    if not target_ids:
        print(f"No pending feedback to process for issue {issue_id}.")
        return 0

    if dismiss:
        registry.mark_feedback_processed(issue_id, target_ids)
        print(f"Dismissed {len(target_ids)} feedback item(s) for issue {issue_id}.")
        return 0

    if approve:
        _write_control(
            "review_followup", issue_id, ",".join(target_ids), workspace_root=workspace_root
        )
        print(f"Approved {len(target_ids)} feedback item(s) for issue {issue_id}.")
        print("Follow-up will be triggered on next orchestrator poll cycle.")
        return 0

    return 0


# ---------------------------------------------------------------------------
# issue diff
# ---------------------------------------------------------------------------


def _run_diff(registry_path: Path | None, args: argparse.Namespace) -> int:
    """Show code changes for an issue using git diff."""
    from pathlib import Path

    issue_id = getattr(args, "id", None)
    if not issue_id:
        print("error: --id is required", file=sys.stderr)
        return 2

    if not registry_path or not registry_path.exists():
        print(f"No registry found. Cannot show diff for issue {issue_id}.", file=sys.stderr)
        return 1

    from orchestratord.issue_registry import IssueRegistry

    registry = IssueRegistry(registry_path)
    record = registry.get(issue_id)
    if record is None:
        print(f"Issue {issue_id} not found in registry.", file=sys.stderr)
        return 1

    branch_name = record.branch_name
    if not branch_name:
        print(f"Issue {issue_id} has no branch name recorded.", file=sys.stderr)
        return 1

    # Resolve workspace path
    workspace_root = getattr(args, "workspace", None)
    if workspace_root is None:
        workspace_root = os.environ.get("ORCHESTRATORD_WORKSPACE_ROOT")

    if not workspace_root:
        print(
            "Cannot resolve workspace root. Set ORCHESTRATORD_WORKSPACE_ROOT or use --workspace.",
            file=sys.stderr,
        )
        return 1

    ws_path = Path(workspace_root)
    if not ws_path.exists():
        print(f"Workspace not found: {ws_path}", file=sys.stderr)
        return 1

    previous_workspace = os.environ.get("ORCHESTRATORD_WORKSPACE_ROOT")
    os.environ["ORCHESTRATORD_WORKSPACE_ROOT"] = str(ws_path)
    try:
        issue_ws = _resolve_issue_workspace_path(issue_id)
    finally:
        if previous_workspace is None:
            os.environ.pop("ORCHESTRATORD_WORKSPACE_ROOT", None)
        else:
            os.environ["ORCHESTRATORD_WORKSPACE_ROOT"] = previous_workspace

    if issue_ws is None:
        for wd in ws_path.iterdir():
            if not wd.is_dir():
                continue
            metadata_file = wd / ".metadata"
            if metadata_file.exists():
                import json

                try:
                    metadata = json.loads(metadata_file.read_text())
                    if metadata.get("issue_id") == issue_id:
                        issue_ws = wd
                        break
                except Exception:
                    pass
            if wd.name == issue_id or issue_id in wd.name:
                issue_ws = wd
                break

    if issue_ws is None:
        print(f"Workspace not found for issue {issue_id}.", file=sys.stderr)
        return 1

    # Check if it's a git repository
    git_dir = issue_ws / ".git"
    if not git_dir.exists():
        # Not a git repo — show file tree instead
        return _show_diff_non_git(issue_ws, issue_id, args)

    import subprocess

    base_branch = record.base_branch or "main"

    # Get agent's run summary from comments (if available)
    agent_summary = _fetch_agent_summary(issue_id, ws_path)

    # Get diff compared to parent commit (this is what the agent actually changed)
    diff_target = _get_diff_target(issue_ws)

    # Get diff stat (summary)
    stat_result = subprocess.run(
        ["git", "diff", "--stat", diff_target],
        cwd=str(issue_ws),
        capture_output=True,
        text=True,
    )

    # Also get the actual diff content
    diff_result = subprocess.run(
        ["git", "diff", "--no-color", diff_target],
        cwd=str(issue_ws),
        capture_output=True,
        text=True,
    )

    show_full = getattr(args, "full", False)
    show_stat_only = getattr(args, "stat", False) and not show_full

    print(f"Issue {issue_id} — Changes")
    print(f"  Branch    : {branch_name}")
    print(f"  Base      : {base_branch}")
    if record.commit_sha:
        print(f"  Commit    : {record.commit_sha[:12]}")
    print()

    # Show agent summary if available
    if agent_summary:
        print("## Agent Summary")
        print(agent_summary)
        print()

    if stat_result.stdout.strip():
        print(stat_result.stdout)

    if show_full and diff_result.stdout.strip():
        print("--- Full Diff ---")
        print(diff_result.stdout)
    elif show_stat_only:
        pass  # stat already printed above
    else:
        # Default: show stat + first 50 lines of diff
        print("--- Diff Preview (use --full for complete output) ---")
        diff_lines = diff_result.stdout.strip().split("\n")
        if len(diff_lines) > 60:
            print("\n".join(diff_lines[:60]))
            print(f"\n  ... ({len(diff_lines) - 60} more lines, use --full to see all)")
        elif diff_lines:
            print("\n".join(diff_lines))

    return 0


def _get_diff_target(ws_path: Path) -> str:
    """Get the diff target (compare HEAD vs its parent commit)."""
    import subprocess

    # Get the parent commit hash
    result = subprocess.run(
        ["git", "rev-parse", "HEAD~1"],
        cwd=str(ws_path),
        capture_output=True,
        text=True,
    )

    if result.returncode == 0:
        parent = result.stdout.strip()
        return f"{parent}...HEAD"

    # If no parent (first commit), show diff of working tree vs empty
    return "HEAD"


def _fetch_agent_summary(issue_id: str, ws_path: Path) -> str | None:
    """Fetch the agent's run summary from issue comments.

    Returns the first "## Orchestratord Run Complete" comment if found,
    otherwise returns None.
    """
    import json
    import re
    from pathlib import Path

    # Pattern to find safe stem for issue
    safe_stem = re.sub(r"[^A-Za-z0-9._-]+", "-", issue_id.strip()).strip("-._")

    # Search in multiple possible locations for comments
    search_dirs = [
        ws_path,  # workspace root
        ws_path.parent / ".orchestratord_local_issues",
        ws_path.parent / ".orchestratord",
    ]

    for comments_dir in search_dirs:
        if not comments_dir.exists():
            continue

        # Find comment files matching this issue
        comment_files = list(comments_dir.glob(f"{safe_stem}*.comments.ndjson"))
        if not comment_files:
            # Also try with the issue directory name
            comment_files = list(comments_dir.glob(f"*{issue_id}*.comments.ndjson"))

        for cf in comment_files:
            try:
                for line in cf.read_text(encoding="utf-8").splitlines():
                    if not line.strip():
                        continue
                    payload = json.loads(line)
                    body = payload.get("body", "")
                    if "## Orchestratord Run Complete" in body:
                        # Extract the output excerpt section
                        if "**Output excerpt:**" in body:
                            idx = body.index("**Output excerpt:**")
                            return body[idx:]
                        elif body:
                            # Return the whole body as summary
                            return body[:500] if len(body) > 500 else body
            except Exception:
                pass

    return None


def _has_origin(ws_path: Path) -> bool:
    """Check if the workspace has an origin remote."""
    import subprocess

    result = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "origin/HEAD"],
        cwd=str(ws_path),
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


def _show_diff_non_git(ws_path: Path, issue_id: str, args: argparse.Namespace) -> int:
    """Show file tree for non-git workspace."""
    print(f"Issue {issue_id} — Workspace Files (not a git repository)")
    print(f"  Workspace: {ws_path}")
    print()

    exclude = {".metadata", ".orchestrator_control", ".operator_hints.md"}

    files: list[tuple[str, str, int]] = []
    dirs: list[str] = []

    for item in sorted(ws_path.iterdir()):
        if item.name in exclude:
            continue
        if item.is_dir():
            dirs.append(item.name + "/")
        else:
            size = item.stat().st_size
            rel_path = item.relative_to(ws_path)
            files.append((str(rel_path), "file", size))

    if not files and not dirs:
        print("  (empty workspace)")
        return 0

    print(f"  {'FILE':<50} {'SIZE':>10}")
    print(f"  {'-' * 50} {'-' * 10}")

    for name, _, size in sorted(files):
        size_str = _format_size(size)
        print(f"  {name:<50} {size_str:>10}")

    for d in dirs:
        print(f"  {d:<50} {'[DIR]':>10}")

    print(f"\n  {len(files)} files, {len(dirs)} directories")
    print("\n  Note: This workspace is not a git repository — no diff available.")
    print(
        "  Use 'orchestratord issue workspace --id {} --cat <file>' to view file contents.".format(
            issue_id
        )
    )
    return 0


def _format_size(size: int) -> str:
    """Format file size in human-readable form."""
    if size < 1024:
        return f"{size}B"
    elif size < 1024 * 1024:
        return f"{size // 1024}KB"
    else:
        return f"{size // (1024 * 1024)}MB"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _get_status_str(status) -> str:
    """Normalize status to string."""
    if hasattr(status, "value"):
        return status.value
    return str(status)


# ---------------------------------------------------------------------------
# issue retry  (CLI 兜底命令)
# ---------------------------------------------------------------------------

# Single source of truth for the on-disk audit log location. Tests
# override this by monkey-patching `_DEFAULT_AUDIT_LOG_PATH` to a
# tempdir, so the production path is the only constant we expose.
_DEFAULT_AUDIT_LOG_PATH = Path.home() / ".orchestratord" / "orchestrator" / "audit.jsonl"


def _resolve_operator(explicit: str | None) -> str:
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
        return "unknown"


def _append_audit_log(
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
    import time

    target = path or _DEFAULT_AUDIT_LOG_PATH
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
        print(
            f"warning: failed to write audit log {target}: {exc}",
            file=sys.stderr,
        )
        return None


def _run_rebase(
    registry_path: Path | None, args: argparse.Namespace, workspace_root: str | Path | None = None
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
        print("error: --id is required for rebase", file=sys.stderr)
        return 2
    force = bool(getattr(args, "force", False))
    reason = getattr(args, "reason", "") or ""
    operator = _resolve_operator(getattr(args, "operator", None))

    if registry_path is None or not registry_path.exists():
        print(
            "error: no issue registry found for this workspace.\n"
            "hint: run from a project root or pass --workspace / --workflow.",
            file=sys.stderr,
        )
        return 1

    from orchestratord.issue_registry import IssueRegistry

    registry = IssueRegistry(registry_path)
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
        print(
            f"error: issue {issue_id} ({record.issue_identifier}) has "
            f"no PR / workspace / branch registered. The rebase path "
            f"requires a previously-opened PR. Run a normal agent "
            f"cycle first to open a PR, then re-issue this command.",
            file=sys.stderr,
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
        print(
            f"warning: issue {issue_id} has reached "
            f"rebase_attempt_count={record.rebase_attempt_count} >= "
            f"max_rebase_attempts_per_issue={max_attempts}. Pass "
            f"--force to bypass (logged as high-priority audit).",
            file=sys.stderr,
        )

    # Mirror the intent label onto the tracker (best-effort).
    tracker = _tracker_from_workflow_arg(args)
    if tracker is not None:
        _mirror_intent_label(tracker, issue_id, "agent:rebase", remove=False)

    # Append a JSONL entry to the local audit log so the operator
    # action is traceable.
    _append_audit_log(
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
    rc = _write_control("rebase", registry_issue_id, extra, workspace_root=workspace_root)
    if rc != 0:
        return rc

    print(f"Issue {issue_id} ({record.issue_identifier}): rebase requested.")
    print(f"  push method: {'--force' if force else '--force-with-lease'}")
    if reason:
        print(f"  reason: {reason}")
    print("  The orchestrator will run `rebase_for_pr` on its next poll cycle (default 30s).")
    return 0


def _run_retry(
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
        print("error: --id is required for retry", file=sys.stderr)
        return 2
    mode = getattr(args, "mode", None)
    if mode not in {"reset", "followup", "unblock"}:
        print(f"error: --mode must be reset|followup|unblock, got {mode!r}", file=sys.stderr)
        return 2
    reason = getattr(args, "reason", "") or ""
    force = bool(getattr(args, "force", False))
    operator = _resolve_operator(getattr(args, "operator", None))
    max_retries = int(getattr(args, "max_retries", 3) or 3)

    if registry_path is None or not registry_path.exists():
        print(
            "error: no issue registry found for this workspace.\n"
            "hint: run from a project root or pass --workspace / --workflow.",
            file=sys.stderr,
        )
        return 1

    from orchestratord.issue_registry import IssueRegistry
    from orchestratord.tracker import Intent

    registry = IssueRegistry(registry_path)
    record = registry.get_by_issue_ref(issue_id)

    # --stop-first: if the agent is still running, stop it
    # before retrying. Equivalent to 'issue stop' + 'issue retry'.
    stop_first = bool(getattr(args, "stop_first", False))
    if stop_first and record is not None and record.status.value == "running":
        sock_path = _resolve_sock_path(issue_id, workspace_root)
        if sock_path is not None:
            print(f"Stopping running agent for {issue_id} before retry…")

            async def _stop_for_retry() -> bool:
                data = await _send_and_wait(sock_path, "stop", "", "SessionComplete", timeout=10.0)
                return data is not None

            stopped = asyncio.run(_stop_for_retry())
            if stopped:
                print("Agent stopped. Proceeding with retry.")
            else:
                print(
                    "warning: stop sent but agent may still be unwinding. "
                    "Proceeding with retry anyway.",
                    file=sys.stderr,
                )
        else:
            print(
                f"warning: could not find control socket for {issue_id}. "
                f"Writing stop control file as fallback.",
                file=sys.stderr,
            )
            _write_control("stop", issue_id, workspace_root=workspace_root)
    elif stop_first and record is not None and record.status.value != "running":
        print(
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
        tracker = _tracker_from_workflow_arg(args)
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

                    asyncio.run(reopen_tracker_issue())
                except Exception as exc:
                    print(f"Warning: could not update tracker: {exc}", file=sys.stderr)
                # Mirror the retry intent onto the remote issue
                # label so label-based intent resolution sees the
                # same intent. Best-effort: the tracker may not
                # implement add_label (returns False), or the API
                # call may fail — both are non-fatal because the
                # local registry.intent is the authoritative
                # source.
                _mirror_intent_label(tracker, issue_id, "agent:retry", remove=False)
            # The CLI may run inside the IM gateway process while the
            # orchestrator daemon keeps a separate, already-loaded
            # IssueRegistry instance. Persisting the registry alone does
            # not update that in-memory state (notably its completed set),
            # so explicitly notify the daemon through its control queue.
            control_root = workspace_root or registry_path.parent
            control_rc = _write_control(
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
                _mirror_intent_label(tracker, issue_id, "agent:follow-up", remove=False)
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
                    _inject_hint(registry_issue_id, ws_path / ".operator_hints.md", reason)
            # Notify the daemon via control file so it picks up the
            # intent immediately instead of waiting for the next poll.
            control_root = workspace_root or registry_path.parent
            control_rc = _write_control(
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
                _mirror_intent_label(tracker, issue_id, "agent:blocked", remove=True)
            action = "unblocked"
        audit_priority = "high" if force else "normal"
        audit_event = "retry" if mode == "reset" else mode

    audit_path = _append_audit_log(
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

    print(f"Issue {issue_id} ({record.issue_identifier}): {action}.")
    if reason:
        print(f"  reason: {reason}")
    print(f"  operator: {operator}")
    if rate_limited:
        print(
            f"  rate limit: retry_count={record.retry_count} >= "
            f"max_retries_per_issue={max_retries}.\n"
            f"  Re-run with --force to bypass (logged as high-priority audit).",
            file=sys.stderr,
        )
    if force and not rate_limited:
        print("  (--force set: rate limit bypassed, audit entry marked high-priority)")
    if audit_path is not None:
        print(f"  audit log: {audit_path}")
    if control_rc == 0:
        print("  The orchestrator will pick this up on its next poll cycle.")
    else:
        print(
            "  The local reset was saved, but the orchestrator control command failed.",
            file=sys.stderr,
        )
    if rate_limited:
        return 3
    return control_rc


# ── issue init ───────────────────────────────────────────────────────


def _run_init(args: argparse.Namespace) -> int:
    """Scaffold an issue card from the issue-card.template.md."""
    from pathlib import Path
    from datetime import datetime, timezone

    template = """# Issue: <TITLE>

- ID: <ID>
- Identifier: <IDENTIFIER>
- State: <STATE>
- Priority: <PRIORITY>
- Category: <CATEGORY_TAG>
- Branch: <BRANCH_NAME>
- Base branch: <BASE_BRANCH>
- Assignee: <ASSIGNEE>
- Upstream URL: <UPSTREAM_URL>
- Created: <ISO8601>

## Description

<!-- Describe the desired change, acceptance criteria, and constraints. -->
"""

    # Determine output path
    out = Path(args.output).expanduser().resolve()
    if out.exists():
        print(f"✗ {out} already exists — remove it first or use --output", file=sys.stderr)
        return 1

    interactive = sys.stdin.isatty() and not args.non_interactive

    def val(flag_val: str, label: str, default: str = "") -> str:
        if flag_val:
            return flag_val
        if interactive:
            try:
                raw = input(f"  {label} [{default}]: ")
                return raw.strip() or default
            except (EOFError, KeyboardInterrupt):
                return default
        return default

    issue_id = val(args.id, "Issue ID (e.g. <ID>-pr-auto-fix)", "")
    identifier = val(args.identifier, "Short identifier (e.g. <id>)", "")
    title = val(args.title, "Issue title", "")
    priority = val(args.priority, "Priority (0-3)", "3")
    state = args.state or "open"
    category = val(args.category, "Category label (e.g. feature, bug, refactor)", "feature")
    branch_name = val(args.branch_name, "Preferred branch name (blank for auto)", "")
    base_branch = val(args.base_branch, "Base branch (e.g. main, dev-decoupling)", "")
    assignee = val(args.assignee, "Assignee / team", "")
    url = val(args.url, "Upstream issue / document URL", "")
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # Read and replace all <...> placeholders
    raw = template
    replacements = {
        "<ID>": issue_id,
        "<IDENTIFIER>": identifier,
        "<TITLE>": title,
        "<PRIORITY>": priority,
        "<STATE>": state,
        "<CATEGORY_TAG>": category,
        "<BRANCH_NAME>": branch_name,
        "<BASE_BRANCH>": base_branch,
        "<ASSIGNEE>": assignee,
        "<UPSTREAM_URL>": url,
        "<ISO8601>": now,
    }
    for key, replacement in replacements.items():
        raw = raw.replace(key, replacement)

    # Write
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(raw, encoding="utf-8")

    remaining = raw.count("<") and raw.count(">")
    print(f"✓ Generated {out}")
    print()
    print("  Next steps:")
    if remaining:
        print(f"    1. Edit {out.name} — review and fill any remaining <...> placeholders")
    else:
        print(f"    1. Review {out.name} — all placeholders have been filled")
    print(f"    2. Move it to your local tracker's issues path")
    print(f"    3. Start: orchestratord server start --workflow workflow.md")
    return 0
