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
  orchestratord issue inject --id <id> <hint | --hint TEXT> [--list] [--remove N]

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
from typing import TYPE_CHECKING, Any

from orchestratord.paths import SESSIONS_DIR

if TYPE_CHECKING:
    from orchestratord.issue_registry.models import IssueRecord


# ---------------------------------------------------------------------------
# Parser registration
# ---------------------------------------------------------------------------


from datetime import UTC

from orchestratord.commands.parsing import add_issue_parser  # noqa: F401

# ---------------------------------------------------------------------------
# Run dispatch
# ---------------------------------------------------------------------------


def run(args: argparse.Namespace) -> int:
    """Dispatch to the appropriate issue subcommand."""
    cmd = args.issue_subcommand

    from orchestratord.commands.cli_adapter import TerminalOutput, render
    from orchestratord.commands.models import CommandRequest
    from orchestratord.commands.service import ISSUE_ACTIONS, OrchestratorCommandService

    if cmd in ISSUE_ACTIONS:
        service = OrchestratorCommandService(
            confirm=input, output_factory=TerminalOutput, timeout_seconds=None
        )
        return render(
            asyncio.run(service.execute(CommandRequest("issue", cmd, vars(args))))
        )

    from orchestratord.workspace_locator import get_registry_path

    registry_path = get_registry_path(
        workspace_arg=getattr(args, "workspace", None),
        workflow_path=getattr(args, "workflow", None),
    )
    if cmd == "tail":
        return _run_tail(registry_path, args)
    if cmd == "transcript":
        return _run_transcript(registry_path, args)
    if cmd == "diff":
        return _run_diff(registry_path, args)
    if cmd == "init":
        return _run_init(args)

    print(f"error: unknown issue subcommand '{cmd}'", file=sys.stderr)
    return 2


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _control_path(workspace_root: str | Path | None = None) -> Path:
    """Compatibility adapter for the shared issue service."""
    return _call_shared("_control_path", workspace_root)


def _resolve_sock_path(
    issue_id: str,
    workspace_root: str | Path | None = None,
) -> Path | None:
    """Compatibility adapter for the shared issue service."""
    return _call_shared("_resolve_sock_path", issue_id, workspace_root)


async def _send_and_wait(
    sock_path: Path,
    cmd: str,
    payload: str,
    expected_type: str,
    timeout: float = 30.0,
) -> dict | None:
    """Compatibility adapter for the shared issue service."""
    return await _call_shared_async(
        "_send_and_wait", sock_path, cmd, payload, expected_type, timeout
    )


def _write_control(
    cmd: str, issue_id: str, extra: str = "", workspace_root: str | Path | None = None
) -> int:
    """Compatibility adapter for the shared issue service."""
    return _call_shared("_write_control", cmd, issue_id, extra, workspace_root)


def _try_socket_inject(issue_id: str, hint: str) -> bool:
    """Compatibility adapter for the shared issue service."""
    return _call_shared("_try_socket_inject", issue_id, hint)


# ---------------------------------------------------------------------------
# issue list
# ---------------------------------------------------------------------------


def _run_list(registry_path: Path | None, args: argparse.Namespace) -> int:
    """Compatibility adapter for the shared issue service."""
    return _call_shared("_run_list", registry_path, args)


# ---------------------------------------------------------------------------
# issue show
# ---------------------------------------------------------------------------


def _run_show(registry_path: Path | None, args: argparse.Namespace) -> int:
    """Compatibility adapter for the shared issue service."""
    return _call_shared("_run_show", registry_path, args)


def _print_session_usage(record: IssueRecord) -> None:
    """Compatibility adapter for the shared issue service."""
    return _call_shared("_print_session_usage", record)


def _resolve_issue_workspace_path(
    issue_id: str,
    workspace_arg: str | None = None,
) -> Path | None:
    """Compatibility adapter for the shared issue service."""
    return _call_shared("_resolve_issue_workspace_path", issue_id, workspace_arg)


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
        # Render the recent backlog before following (like `tail -n N -f`).
        # Starting from a bare EOF made finished runs look hung on an
        # empty screen.
        backlog = getattr(args, "lines", None)
        backlog = 50 if backlog is None else int(backlog)
        pending_calls: dict[str, dict] = {}
        turn_counter = 0
        if backlog != 0:
            raw = transcript_path.read_bytes()
            if raw and not raw.endswith(b"\n"):
                # Trailing partial line: skip here; the follow loop renders
                # it once the writer completes it.
                raw = raw[: raw.rfind(b"\n") + 1]
            all_lines = raw.decode("utf-8", errors="replace").splitlines()
            selected = all_lines if backlog < 0 else all_lines[-backlog:]
            for line in selected:
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
            if selected:
                print(
                    f"[tail] shown last {len(selected)} of {len(all_lines)} entries — "
                    "following new ones"
                )
            last_size = len(raw)
        else:
            last_size = transcript_path.stat().st_size

        pending = ""
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


def _format_ts(timestamp_value: float | str | None) -> str:
    """Format a transcript entry timestamp to ``HH:MM:SS``.

    SessionStorage (schema v2) writes Unix epoch floats; legacy ISO-8601
    strings are also accepted. Falls back to the current local time when
    the entry has no usable timestamp (legacy records, session_snapshot
    lines, etc.).
    """
    from datetime import datetime

    if timestamp_value is not None:
        try:
            if isinstance(timestamp_value, (int, float)):
                return datetime.fromtimestamp(timestamp_value).strftime("%H:%M:%S")
            text = str(timestamp_value).strip()
            if text.replace(".", "", 1).isdigit():
                # Epoch seconds serialized as a string.
                return datetime.fromtimestamp(float(text)).strftime("%H:%M:%S")
            return datetime.fromisoformat(text).strftime("%H:%M:%S")
        except (ValueError, TypeError, OSError, OverflowError):
            pass
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
            elif (
                "pr opened" in lower or "pull request" in lower or "opened pr" in lower
            ):
                preview = text[:80].replace("\n", " ")
                print(f"{ts}  ✓ PR opened · {preview.strip()}")
            # Git operations
            elif (
                lower.startswith("git")
                or "git commit" in lower
                or "committed" in lower
                or "push" in lower
                and ("git" in lower or "origin" in lower)
            ):
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
            if (
                btype == "tool_result"
                and block.get("tool_use_id") != tool_use_id_filter
            ):
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
    """Compatibility adapter for the shared issue service."""
    return _call_shared("_run_stop", args, registry_path, workspace_root)


# ---------------------------------------------------------------------------
# issue pause
# ---------------------------------------------------------------------------


def _run_pause(
    args: argparse.Namespace, workspace_root: str | Path | None = None
) -> int:
    """Compatibility adapter for the shared issue service."""
    return _call_shared("_run_pause", args, workspace_root)


# ---------------------------------------------------------------------------
# issue resume
# ---------------------------------------------------------------------------


def _run_resume(
    args: argparse.Namespace, workspace_root: str | Path | None = None
) -> int:
    """Compatibility adapter for the shared issue service."""
    return _call_shared("_run_resume", args, workspace_root)


# ---------------------------------------------------------------------------
# issue clarify
# ---------------------------------------------------------------------------


def _run_clarify(
    args: argparse.Namespace,
    *,
    registry_path: Path | None = None,
    workspace_root: Path | None = None,
) -> int:
    """Compatibility adapter for the shared issue service."""
    return _call_shared(
        "_run_clarify", args, registry_path=registry_path, workspace_root=workspace_root
    )


# ---------------------------------------------------------------------------
# issue inject
# ---------------------------------------------------------------------------


def _run_inject(args: argparse.Namespace) -> int:
    """Compatibility adapter for the shared issue service."""
    return _call_shared("_run_inject", args)


def _parse_hints_file(hints_file: Path) -> list[tuple[float, str]]:
    """Compatibility adapter for the shared issue service."""
    return _call_shared("_parse_hints_file", hints_file)


def _inject_hint(issue_id: str, hints_file: Path, hint: str) -> int:
    """Compatibility adapter for the shared issue service."""
    return _call_shared("_inject_hint", issue_id, hints_file, hint)


def _list_hints(issue_id: str, hints_file: Path) -> int:
    """Compatibility adapter for the shared issue service."""
    return _call_shared("_list_hints", issue_id, hints_file)


def _remove_hint(issue_id: str, hints_file: Path, hint_num: int) -> int:
    """Compatibility adapter for the shared issue service."""
    return _call_shared("_remove_hint", issue_id, hints_file, hint_num)


# ---------------------------------------------------------------------------
# issue workspace
# ---------------------------------------------------------------------------


def _run_workspace(args: argparse.Namespace) -> int:
    """Compatibility adapter for the shared issue service."""
    return _call_shared("_run_workspace", args)


def _workspace_list_files(issue_id: str, ws_path: Path) -> int:
    """Compatibility adapter for the shared issue service."""
    return _call_shared("_workspace_list_files", issue_id, ws_path)


def _workspace_cat_file(issue_id: str, ws_path: Path, filename: str) -> int:
    """Compatibility adapter for the shared issue service."""
    return _call_shared("_workspace_cat_file", issue_id, ws_path, filename)


def _workspace_edit_file(
    issue_id: str, ws_path: Path, filename: str, content: str
) -> int:
    """Compatibility adapter for the shared issue service."""
    return _call_shared("_workspace_edit_file", issue_id, ws_path, filename, content)


# ---------------------------------------------------------------------------
# issue review
# ---------------------------------------------------------------------------


def _tracker_from_workflow_arg(args: argparse.Namespace) -> Any | None:
    """Compatibility adapter for the shared issue service."""
    return _call_shared("_tracker_from_workflow_arg", args)


def _mirror_intent_label(
    tracker: Any | None,
    issue_id: str,
    label: str,
    *,
    remove: bool,
) -> bool:
    """Compatibility adapter for the shared issue service."""
    return _call_shared("_mirror_intent_label", tracker, issue_id, label, remove=remove)


def _run_review(
    registry_path: Path | None,
    args: argparse.Namespace,
    workspace_root: str | Path | None = None,
) -> int:
    """Compatibility adapter for the shared issue service."""
    return _call_shared("_run_review", registry_path, args, workspace_root)


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
    """Compatibility adapter for the shared issue service."""
    return _call_shared("_fallback_feedback_url", record, feedback_id)


def _run_feedback(
    registry_path: Path | None,
    args: argparse.Namespace,
    workspace_root: str | Path | None = None,
) -> int:
    """Compatibility adapter for the shared issue service."""
    return _call_shared("_run_feedback", registry_path, args, workspace_root)


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
        print(
            f"No registry found. Cannot show diff for issue {issue_id}.",
            file=sys.stderr,
        )
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
    import re

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
        f"  Use 'orchestratord issue workspace --id {issue_id} --cat <file>' to view file contents."
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
    """Compatibility adapter for the shared issue service."""
    return _call_shared("_get_status_str", status)


# ---------------------------------------------------------------------------
# issue retry  (CLI 兜底命令)
# ---------------------------------------------------------------------------

# Single source of truth for the on-disk audit log location. Tests
# override this by monkey-patching `_DEFAULT_AUDIT_LOG_PATH` to a
# tempdir, so the production path is the only constant we expose.
_DEFAULT_AUDIT_LOG_PATH = (
    Path.home() / ".orchestratord" / "orchestrator" / "audit.jsonl"
)


def _resolve_operator(explicit: str | None) -> str:
    """Compatibility adapter for the shared issue service."""
    return _call_shared("_resolve_operator", explicit)


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
    """Compatibility adapter for the shared issue service."""
    return _call_shared(
        "_append_audit_log",
        issue_id=issue_id,
        mode=mode,
        reason=reason,
        operator=operator,
        force=force,
        extra=extra,
        path=path,
    )


def _run_rebase(
    registry_path: Path | None,
    args: argparse.Namespace,
    workspace_root: str | Path | None = None,
) -> int:
    """Compatibility adapter for the shared issue service."""
    return _call_shared("_run_rebase", registry_path, args, workspace_root)


def _run_retry(
    registry_path: Path | None,
    args: argparse.Namespace,
    *,
    workspace_root: str | Path | None = None,
) -> int:
    """Compatibility adapter for the shared issue service."""
    return _call_shared(
        "_run_retry", registry_path, args, workspace_root=workspace_root
    )


# ── issue init ───────────────────────────────────────────────────────


def _run_init(args: argparse.Namespace) -> int:
    """Scaffold an issue card from the issue-card.template.md."""
    from datetime import datetime
    from pathlib import Path

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
        print(
            f"✗ {out} already exists — remove it first or use --output", file=sys.stderr
        )
        return 1

    interactive = sys.stdin.isatty() and not args.non_interactive

    def val(flag_val: str, label: str, default: str = "") -> str:
        if flag_val:
            return flag_val
        if interactive:
            try:
                raw = input(f"  {label} [{default}]: ")
                return raw.strip() or default
            except EOFError:
                # Ctrl+D falls back to the default; Ctrl+C propagates to abort.
                return default
        return default

    issue_id = val(args.id, "Issue ID (e.g. <ID>-pr-auto-fix)", "")
    identifier = val(args.identifier, "Short identifier (e.g. <id>)", "")
    title = val(args.title, "Issue title", "")
    priority = val(args.priority, "Priority (0-3)", "3")
    state = args.state or "open"
    category = val(
        args.category, "Category label (e.g. feature, bug, refactor)", "feature"
    )
    branch_name = val(args.branch_name, "Preferred branch name (blank for auto)", "")
    base_branch = val(args.base_branch, "Base branch (e.g. main, dev-decoupling)", "")
    assignee = val(args.assignee, "Assignee / team", "")
    url = val(args.url, "Upstream issue / document URL", "")
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

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
        print(
            f"    1. Edit {out.name} — review and fill any remaining <...> placeholders"
        )
    else:
        print(f"    1. Review {out.name} — all placeholders have been filled")
    print("    2. Move it to your local tracker's issues path")
    print("    3. Start: orchestratord server start --workflow workflow.md")
    return 0


def _shared_context():
    from orchestratord.commands.models import CommandContext

    return CommandContext(
        confirm=input,
        audit_path=_DEFAULT_AUDIT_LOG_PATH,
    )


def _call_shared(name, *args, **kwargs):
    from orchestratord.commands import issue as operations
    from orchestratord.commands.cli_adapter import invoke

    return invoke(getattr(operations, name), _shared_context(), *args, **kwargs)


async def _call_shared_async(name, *args, **kwargs):
    from orchestratord.commands import issue as operations
    from orchestratord.commands.cli_adapter import invoke_async

    return await invoke_async(
        getattr(operations, name), _shared_context(), *args, **kwargs
    )
