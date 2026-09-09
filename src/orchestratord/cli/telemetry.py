"""``orchestratord telemetry`` — manual telemetry reporting.

One verb:

* ``report`` — aggregate local telemetry event files and push each
  covered day to its remote GitCode issue. This is the same path the
  daemon takes after each agent run (``kernel.telemetry.report_telemetry``),
  exposed so missed days can be backfilled on demand:

      orchestratord telemetry report --workflow WORKFLOW.md --days 7
      orchestratord telemetry report --workflow WORKFLOW.md --days all

``--days`` overrides ``telemetry.backfill_days`` from the workflow
config (default when unspecified: 1 = today only). Days never reported,
or last reported before they ended (mid-day upload), are (re-)reported;
days with a complete remote report are skipped.
"""

from __future__ import annotations

import argparse
import sys
from typing import Any


def add_telemetry_parser(subparsers: argparse._SubParsersAction) -> None:
    """Register the ``telemetry`` subcommand and its verbs."""
    parser = subparsers.add_parser(
        "telemetry",
        help="Telemetry reporting utilities",
        description="Push aggregated local telemetry to the remote issue tracker.",
    )
    sub = parser.add_subparsers(dest="telemetry_subcommand", required=True)
    report = sub.add_parser(
        "report",
        help="Push daily summaries to the remote telemetry issue(s)",
    )
    report.add_argument(
        "--days",
        default=None,
        help="Days to cover: an integer N (last N days incl. today) or 'all' "
        "(every local day). Default: telemetry.backfill_days from the "
        "workflow config, else 1 (today only).",
    )
    report.add_argument(
        "--workflow",
        default=None,
        help="Workflow file providing owner/repo/title/api_key "
        "(telemetry section, falling back to tracker auth)",
    )
    report.add_argument("--owner", default=None, help="Override report owner")
    report.add_argument("--repo", default=None, help="Override report repo")
    report.add_argument("--title", default=None, help="Override issue title")
    report.add_argument(
        "--api-key", dest="api_key", default=None, help="Override API key"
    )


def _parse_days(raw: str | None) -> int | str | None:
    """``--days`` value → None (unset), "all", or int. Raises ValueError."""
    if raw is None:
        return None
    text = str(raw).strip().lower()
    if text == "all":
        return "all"
    value = int(text)
    if value < 1:
        raise ValueError("days must be >= 1 or 'all'")
    return value


def _resolve_target(args: argparse.Namespace) -> dict[str, Any]:
    """Merge workflow telemetry config with explicit CLI overrides."""
    owner = str(args.owner or "")
    repo = str(args.repo or "")
    title = str(args.title or "")
    api_key = str(args.api_key or "")
    days: int | str | None = _parse_days(args.days)

    if args.workflow:
        from orchestratord.workflow import WorkflowLoader

        workflow, _ = WorkflowLoader.load(args.workflow)
        tele = getattr(workflow, "telemetry", None)
        tracker = getattr(workflow, "tracker", None)
        owner = owner or (getattr(tele, "report_owner", "") or getattr(tracker, "owner", "") or "")
        repo = repo or (getattr(tele, "report_repo", "") or getattr(tracker, "repo", "") or "")
        api_key = api_key or (getattr(tele, "api_key", "") or getattr(tracker, "api_key", "") or "")
        title = title or (getattr(tele, "issue_title", "") or "")
        if days is None:
            days = getattr(tele, "backfill_days", 1)

    return {
        "owner": owner,
        "repo": repo,
        "title": title or "Orchestratord Telemetry",
        "api_key": api_key,
        "days": days if days is not None else 1,
    }


def _run_report(args: argparse.Namespace) -> int:
    try:
        days = _parse_days(args.days)
    except ValueError:
        print("error: --days must be an integer >= 1 or 'all'", file=sys.stderr)
        return 2

    try:
        target = _resolve_target(args)
    except Exception as exc:
        print(f"error: cannot load workflow {args.workflow}: {exc}", file=sys.stderr)
        return 1
    if days is not None:
        target["days"] = days

    missing = [
        name
        for name, value in (
            ("--owner", target["owner"]),
            ("--repo", target["repo"]),
            ("--api-key", target["api_key"]),
        )
        if not value
    ]
    if missing:
        print(
            f"error: missing {' or '.join(missing)} (or pass --workflow with "
            "telemetry/tracker config)",
            file=sys.stderr,
        )
        return 1

    from orchestratord.telemetry.reporters import report_backfill

    results = report_backfill(
        owner=target["owner"],
        repo=target["repo"],
        api_key=target["api_key"],
        title=target["title"],
        days=target["days"],
    )
    if not results:
        print("Nothing to report — all covered days already pushed.")
        return 0
    failed = 0
    for day, ok, detail in results:
        print(f"{'✓' if ok else '✗'} {day}: {detail}")
        if not ok:
            failed += 1
    return 1 if failed else 0


def run(args: argparse.Namespace) -> int:
    if args.telemetry_subcommand == "report":
        return _run_report(args)
    print("error: unknown telemetry subcommand", file=sys.stderr)
    return 2
