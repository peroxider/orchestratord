"""Generic workflow-run lifecycle commands."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
import uuid
from pathlib import Path


def _add_location(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--workspace", default=None, metavar="PATH")
    parser.add_argument("--workflow-config", dest="workflow", default=None, metavar="WORKFLOW_MD")


def add_run_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("run", help="Start and control workflow runs")
    commands = parser.add_subparsers(dest="run_subcommand", required=True)

    start = commands.add_parser("start", help="Execute a declarative workflow once")
    start.add_argument("workflow_file", metavar="WORKFLOW_YAML")
    start.add_argument("--backend", metavar="NAME", help="Required when the workflow has agent stages")
    start.add_argument("--config", dest="runtime_config", metavar="WORKFLOW_MD")
    start.add_argument("--input", default="{}", metavar="JSON")
    start.add_argument("--input-file", metavar="JSON_FILE")
    start.add_argument("--workspace", default=".", metavar="PATH")
    start.add_argument("--max-concurrency", type=int, default=1, metavar="N")

    listing = commands.add_parser("list", help="List known runs")
    listing.add_argument("--status")
    _add_location(listing)

    show = commands.add_parser("show", help="Show a run or legacy issue-backed run")
    show.add_argument("--id", required=True, metavar="RUN_OR_ISSUE_ID")
    _add_location(show)

    logs = commands.add_parser("logs", help="Print a run transcript")
    logs.add_argument("--id", required=True, metavar="RUN_ID")
    logs.add_argument("--role", choices=["user", "assistant"])
    logs.add_argument("--tool-use-id", dest="tool_use_id")
    logs.add_argument("--limit", type=int)
    _add_location(logs)

    cancel = commands.add_parser("cancel", aliases=["stop"], help="Cancel a running run")
    cancel.add_argument("--id", required=True, metavar="RUN_OR_ISSUE_ID")
    cancel.add_argument("--yes", "-y", action="store_true")
    cancel.add_argument("--no-wait", action="store_true")
    _add_location(cancel)

    pause = commands.add_parser("pause", help="Pause a running run")
    pause.add_argument("--id", required=True)
    pause.add_argument("--reason", default="")
    pause.add_argument("--no-wait", action="store_true")
    _add_location(pause)

    resume = commands.add_parser("resume", help="Resume a paused run")
    resume.add_argument("--id", required=True)
    resume.add_argument("--no-wait", action="store_true")
    _add_location(resume)

    inject = commands.add_parser("inject", help="Inject an operator message")
    inject.add_argument("--id", required=True)
    inject.add_argument("message", nargs="?")
    inject.add_argument("--list", dest="list_hints", action="store_true")
    inject.add_argument("--remove", dest="remove_hint", type=int)
    inject.add_argument("--no-wait", action="store_true")
    _add_location(inject)


def _load_input(args: argparse.Namespace) -> dict:
    raw = Path(args.input_file).read_text(encoding="utf-8") if args.input_file else args.input
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("run input must be a JSON object")
    return data


async def _start(args: argparse.Namespace) -> int:
    from orchestratord.agent.task import AgentTask
    from orchestratord.backend_registry import resolve_backend
    from orchestratord.backend_runner import BackendRunner
    from orchestratord.config.schema import WorkflowConfig
    from orchestratord.workflow import WorkflowLoader
    from orchestratord.workflow_runtime import WorkflowRunner
    from orchestratord.run_store import RunRecord, RunStore

    data = _load_input(args)
    if args.runtime_config:
        config, _ = WorkflowLoader.load(args.runtime_config)
    else:
        config = WorkflowConfig()
    workspace = Path(args.workspace).expanduser().resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    config.workspace.root = str(workspace)
    run_id = str(data.get("run_id") or uuid.uuid4())
    store = RunStore(workspace)
    runner = None
    if args.backend:
        backend = resolve_backend(args.backend, strict=True)
        runner = BackendRunner(backend, config.agent, config.sandbox, config.workspace)
    orchestrator = WorkflowRunner(
        args.workflow_file,
        task_runner=runner,
        workspace_dir=workspace,
        max_cost_usd=float(getattr(config.agent, "cost_budget_usd", 50.0) or 50.0),
        max_concurrent_stages=max(1, args.max_concurrency),
        default_timeout_seconds=int(config.agent.run_timeout_ms / 1000),
        control_state=lambda: (
            stored.status if (stored := store.get(run_id)) is not None else None
        ),
    )
    task = AgentTask(
        id=str(data.get("id") or "manual-run"),
        kind=str(data.get("kind") or "generic"),
        title=str(data.get("title") or "Manual workflow run"),
        description=str(data.get("description") or ""),
        context=dict(data.get("context") or {}),
        workspace_path=str(workspace),
        labels=list(data.get("labels") or []),
        prompt_override=data.get("prompt_override"),
    )
    record = RunRecord(
        run_id=run_id,
        workflow=orchestrator.schema.name,
        task_id=task.id,
        task_kind=task.kind,
    )
    store.save(record)
    try:
        result = await orchestrator.run(task)
    except BaseException as exc:
        record.status = "failed"
        record.finished_at = time.time()
        record.error = str(exc)
        store.save(record)
        raise
    latest = store.get(run_id)
    if latest is not None and latest.status == "cancel_requested":
        record.status = "cancelled"
    else:
        record.status = "completed" if result.success else "failed"
    record.finished_at = time.time()
    record.completed_stages = result.completed_stages
    record.total_stages = result.total_stages
    record.cost_usd = result.total_cost_usd
    record.error = result.error
    store.save(record)
    print(json.dumps({
        "run_id": run_id,
        "success": result.success,
        "workflow": result.workflow_name,
        "completed_stages": result.completed_stages,
        "total_stages": result.total_stages,
        "cost_usd": result.total_cost_usd,
        "error": result.error,
    }, indent=2))
    return 0 if result.success else 1


def _legacy(args: argparse.Namespace, command: str) -> int:
    from orchestratord.cli.issue import run as run_issue

    args.issue_subcommand = command
    if command == "transcript":
        args.run = args.id
        args.id = None
    if command == "inject":
        args.hint = args.message
    return run_issue(args)


def _list_runs(args: argparse.Namespace) -> int:
    from orchestratord.run_store import RunStore

    records = RunStore(args.workspace or ".").list(args.status)
    if not records:
        return _legacy(args, "list")
    print(f"{'RUN ID':<38} {'STATUS':<12} {'WORKFLOW':<24} TASK")
    for record in records:
        print(f"{record.run_id:<38} {record.status:<12} {record.workflow:<24} {record.task_id}")
    return 0


def _show_run(args: argparse.Namespace) -> int:
    from dataclasses import asdict
    from orchestratord.run_store import RunStore

    record = RunStore(args.workspace or ".").get(args.id)
    if record is None:
        return _legacy(args, "show")
    print(json.dumps(asdict(record), indent=2))
    return 0


def _control_run(args: argparse.Namespace, status: str) -> int | None:
    from orchestratord.run_store import RunStore

    store = RunStore(args.workspace or ".")
    record = store.get(args.id)
    if record is None:
        return None
    if record.status in ("completed", "failed", "cancelled"):
        print(f"run {record.run_id} is already {record.status}")
        return 0
    store.set_status(record.run_id, status)
    print(f"run {record.run_id}: {status}")
    return 0


def run(args: argparse.Namespace) -> int:
    command = args.run_subcommand
    if command == "start":
        try:
            return asyncio.run(_start(args))
        except Exception as exc:
            print(f"run failed: {exc}", file=sys.stderr)
            return 1
    if command == "list":
        return _list_runs(args)
    if command == "show":
        return _show_run(args)
    control_status = {
        "cancel": "cancel_requested", "stop": "cancel_requested",
        "pause": "paused", "resume": "running",
    }.get(command)
    if control_status is not None:
        result = _control_run(args, control_status)
        if result is not None:
            return result
    mapping = {
        "list": "list", "show": "show", "logs": "transcript",
        "cancel": "stop", "stop": "stop", "pause": "pause",
        "resume": "resume", "inject": "inject",
    }
    return _legacy(args, mapping[command])
