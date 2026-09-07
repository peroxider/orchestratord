from __future__ import annotations

import argparse
import asyncio
import builtins
from pathlib import Path

import pytest


def test_templates_are_packaged_and_discoverable():
    from orchestratord.cli.workflow import _available_templates

    templates = _available_templates()
    assert {"workflow", "workflow-local", "workflow.yaml"} <= set(templates)


def test_workflow_init_local_infers_tracker_kind(tmp_path: Path):
    from orchestratord.cli.workflow import add_workflow_parser, run
    from orchestratord.workflow import WorkflowLoader

    parser = argparse.ArgumentParser()
    subs = parser.add_subparsers(dest="top", required=True)
    add_workflow_parser(subs)
    output = tmp_path / "WORKFLOW.md"
    args = parser.parse_args([
        "workflow", "init", "--template", "workflow-local",
        "--non-interactive", "--output", str(output),
    ])
    assert run(args) == 0
    config, _ = WorkflowLoader.load(output)
    assert config.tracker.kind == "local"


def test_workflow_init_local_kind_on_default_template_autoswitches(tmp_path: Path, capsys):
    from orchestratord.cli.workflow import add_workflow_parser, run
    from orchestratord.workflow import WorkflowLoader

    parser = argparse.ArgumentParser()
    subs = parser.add_subparsers(dest="top", required=True)
    add_workflow_parser(subs)
    output = tmp_path / "WORKFLOW.md"
    args = parser.parse_args([
        "workflow", "init", "--kind", "local",
        "--non-interactive", "--output", str(output),
    ])
    # Default template is `workflow`; picking `local` must auto-switch to
    # workflow-local instead of failing.
    assert run(args) == 0
    assert "using 'workflow-local'" in capsys.readouterr().out
    config, _ = WorkflowLoader.load(output)
    assert config.tracker.kind == "local"


def test_workflow_init_remote_kind_on_local_template_autoswitches(tmp_path: Path):
    from orchestratord.cli.workflow import add_workflow_parser, run

    parser = argparse.ArgumentParser()
    subs = parser.add_subparsers(dest="top", required=True)
    add_workflow_parser(subs)
    output = tmp_path / "WORKFLOW.md"
    args = parser.parse_args([
        "workflow", "init", "--template", "workflow-local", "--kind", "github",
        "--owner", "o", "--repo", "r",
        "--non-interactive", "--output", str(output),
    ])
    assert run(args) == 0  # auto-switches to the `workflow` remote template
    text = output.read_text(encoding="utf-8")
    assert "https://github.com/o/r.git" in text  # clone domain from TrackerKindInfo
    assert "GITHUB_TOKEN" in text  # token env from TrackerKindInfo


def test_workflow_init_rejects_unknown_kind(tmp_path: Path):
    from orchestratord.cli.workflow import add_workflow_parser, run

    parser = argparse.ArgumentParser()
    subs = parser.add_subparsers(dest="top", required=True)
    add_workflow_parser(subs)
    output = tmp_path / "WORKFLOW.md"
    args = parser.parse_args([
        "workflow", "init", "--kind", "nonsense",
        "--non-interactive", "--output", str(output),
    ])
    assert run(args) == 1
    assert not output.exists()


def test_workflow_init_interactive_offers_all_kinds_and_autoswitches(tmp_path: Path, monkeypatch, capsys):
    from orchestratord.cli import workflow as wf
    from orchestratord.workflow import WorkflowLoader

    monkeypatch.setattr(wf.sys.stdin, "isatty", lambda: True)
    asked: list[str] = []

    def fake_prompt(label, default="", secret=False):
        asked.append(label)
        return "local" if label.startswith("Tracker kind") else ""

    monkeypatch.setattr(wf, "_prompt", fake_prompt)

    parser = argparse.ArgumentParser()
    subs = parser.add_subparsers(dest="top", required=True)
    wf.add_workflow_parser(subs)
    output = tmp_path / "WORKFLOW.md"
    args = parser.parse_args(["workflow", "init", "--output", str(output)])
    assert wf.run(args) == 0

    out = capsys.readouterr().out
    # Unified prompt offers every registry kind (local last, registry order)
    assert asked[0] == "Tracker kind (gitcode/gitee/github/linear/local)"
    assert "using 'workflow-local'" in out
    # The local flow never asks repository-hosting questions
    assert not any("Upstream repository owner" in label for label in asked)
    assert any("Issues path (local tracker)" in label for label in asked)
    config, _ = WorkflowLoader.load(output)
    assert config.tracker.kind == "local"


def test_workflow_init_ctrl_c_aborts_interactive_prompt(tmp_path: Path, monkeypatch):
    from orchestratord.cli import workflow as wf

    monkeypatch.setattr(wf.sys.stdin, "isatty", lambda: True)

    def interrupt(prompt=""):
        raise KeyboardInterrupt()

    monkeypatch.setattr(builtins, "input", interrupt)
    parser = argparse.ArgumentParser()
    subs = parser.add_subparsers(dest="top", required=True)
    wf.add_workflow_parser(subs)
    output = tmp_path / "WORKFLOW.md"
    args = parser.parse_args(["workflow", "init", "--output", str(output)])
    with pytest.raises(KeyboardInterrupt):
        wf.run(args)
    assert not output.exists()


def test_workflow_init_ctrl_d_falls_back_to_default(tmp_path: Path):
    from orchestratord.cli import workflow as wf

    wf.sys.stdin.isatty = lambda: True

    def eof(prompt=""):
        raise EOFError()

    saved_input = builtins.input
    builtins.input = eof
    try:
        parser = argparse.ArgumentParser()
        subs = parser.add_subparsers(dest="top", required=True)
        wf.add_workflow_parser(subs)
        output = tmp_path / "WORKFLOW.md"
        args = parser.parse_args(["workflow", "init", "--output", str(output)])
        assert wf.run(args) == 0
        assert 'kind: "github"' in output.read_text(encoding="utf-8")
    finally:
        builtins.input = saved_input


def test_cli_app_converts_keyboard_interrupt_to_exit_130(tmp_path: Path, monkeypatch, capsys):
    from orchestratord.cli import main as cli_main
    import orchestratord.cli.workflow as wf_mod

    def interrupted(args):
        raise KeyboardInterrupt()

    monkeypatch.setattr(wf_mod, "run", interrupted)
    monkeypatch.setattr(
        cli_main.sys, "argv",
        ["orchestratord", "workflow", "init", "--output", str(tmp_path / "w.md")],
    )
    with pytest.raises(SystemExit) as excinfo:
        cli_main.app()
    assert excinfo.value.code == 130
    assert "Interrupted" in capsys.readouterr().err


def _write_fake_transcript(run_dir: Path) -> None:
    import json

    events = [
        {"role": "assistant", "content": [{"type": "text", "text": "hello backlog"}],
         "timestamp": "2026-09-07T10:00:00"},
        {"role": "assistant",
         "content": [{"type": "tool_use", "id": "t1", "name": "Bash",
                      "input": {"command": "pytest -q"}}],
         "timestamp": "2026-09-07T10:00:01"},
        {"role": "user",
         "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "1 passed"}],
         "timestamp": "2026-09-07T10:00:02"},
    ]
    run_dir.mkdir(parents=True)
    (run_dir / "transcript.jsonl").write_text(
        "\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8"
    )


def test_issue_tail_renders_backlog_before_following(tmp_path: Path, monkeypatch, capsys):
    import time as time_mod

    from orchestratord.cli import issue as issue_mod

    _write_fake_transcript(tmp_path / "run-1")
    monkeypatch.setattr(issue_mod, "SESSIONS_DIR", tmp_path)

    def stop_waiting(seconds):
        raise KeyboardInterrupt()

    monkeypatch.setattr(time_mod, "sleep", stop_waiting)

    args = argparse.Namespace(id=None, run="run-1", workspace=None, lines=2, turn=None)
    assert issue_mod._run_tail(None, args) == 0
    out = capsys.readouterr().out
    assert "shown last 2 of 3" in out
    assert "pytest -q" in out  # backlog tool call rendered
    assert "hello backlog" not in out  # older entry not selected
    assert "[tail] stopped" in out


def test_issue_tail_lines_zero_keeps_follow_only_behavior(tmp_path: Path, monkeypatch, capsys):
    import time as time_mod

    from orchestratord.cli import issue as issue_mod

    _write_fake_transcript(tmp_path / "run-1")
    monkeypatch.setattr(issue_mod, "SESSIONS_DIR", tmp_path)

    def stop_waiting(seconds):
        raise KeyboardInterrupt()

    monkeypatch.setattr(time_mod, "sleep", stop_waiting)

    args = argparse.Namespace(id=None, run="run-1", workspace=None, lines=0, turn=None)
    assert issue_mod._run_tail(None, args) == 0
    out = capsys.readouterr().out
    assert "pytest -q" not in out
    assert "shown last" not in out
    assert "[tail] stopped" in out


def test_format_ts_accepts_epoch_floats_and_iso_strings():
    from datetime import datetime

    from orchestratord.cli.issue import _format_ts

    epoch = 1000000000.0  # 2001-09-09, safely in the past
    expected = datetime.fromtimestamp(epoch).strftime("%H:%M:%S")
    assert _format_ts(epoch) == expected
    assert _format_ts(str(epoch)) == expected  # epoch serialized as a string
    assert _format_ts("2026-09-07T10:00:00") == "10:00:00"
    assert _format_ts(None) == datetime.now().strftime("%H:%M:%S")


def test_issue_tail_backlog_shows_original_event_timestamps(tmp_path: Path, monkeypatch, capsys):
    """Replayed backlog lines must carry the event's own timestamp.

    SessionStorage schema v2 writes Unix epoch floats; a regression here
    silently re-stamped every replayed line with render time, so
    re-running tail changed all timestamps while content stayed fixed.
    """
    import json
    import time as time_mod
    from datetime import datetime

    from orchestratord.cli import issue as issue_mod

    epoch_call = 1000000000.0
    run_dir = tmp_path / "run-1"
    run_dir.mkdir(parents=True)
    events = [
        {"role": "assistant",
         "content": [{"type": "tool_use", "id": "t1", "name": "Bash",
                      "input": {"command": "pytest -q"}}],
         "timestamp": epoch_call},
        {"role": "user",
         "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "1 passed"}],
         "timestamp": epoch_call + 2},
    ]
    (run_dir / "transcript.jsonl").write_text(
        "\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8"
    )
    monkeypatch.setattr(issue_mod, "SESSIONS_DIR", tmp_path)

    def stop_waiting(seconds):
        raise KeyboardInterrupt()

    monkeypatch.setattr(time_mod, "sleep", stop_waiting)

    args = argparse.Namespace(id=None, run="run-1", workspace=None, lines=2, turn=None)
    assert issue_mod._run_tail(None, args) == 0
    out = capsys.readouterr().out
    call_ts = datetime.fromtimestamp(epoch_call).strftime("%H:%M:%S")
    assert f"{call_ts}  ◐ Bash pytest -q · 1 passed" in out


def test_generic_cli_resources_parse():
    from orchestratord.cli.app import add_app_parser
    from orchestratord.cli.backend import add_backend_parser
    from orchestratord.cli.run import add_run_parser
    from orchestratord.cli.server import add_server_parser

    parser = argparse.ArgumentParser()
    subs = parser.add_subparsers(dest="resource", required=True)
    add_server_parser(subs, command_name="daemon", dest="daemon_subcommand")
    add_run_parser(subs)
    add_backend_parser(subs)
    add_app_parser(subs)

    assert parser.parse_args(["daemon", "status"]).daemon_subcommand == "status"
    assert parser.parse_args(["run", "logs", "--id", "r1"]).run_subcommand == "logs"
    assert parser.parse_args(["backend", "list"]).backend_subcommand == "list"
    assert parser.parse_args(["app", "list"]).app_subcommand == "list"


def test_action_stage_executes_registered_action(tmp_path: Path):
    from orchestratord.workflow_engine.actions import ActionResult, register_action
    from orchestratord.workflow_engine.engine import DeclarativeWorkflowEngine, WorkflowSchema
    from orchestratord.workflow_engine.stage_runner import StageRunner

    async def action(config, context):
        return ActionResult(success=True, outputs=[config["value"]], artifacts={"root": context.workspace_dir})

    register_action("test.echo", action)
    schema = WorkflowSchema.from_dict({
        "name": "actions",
        "stages": [{"id": 1, "name": "echo", "uses": "test.echo", "with": {"value": "ok"}}],
    })
    engine = DeclarativeWorkflowEngine(schema)
    engine.set_stage_runner(StageRunner(
        agent_runner=None,
        task_runner=None,
        workflow_config=None,
        workspace_dir=str(tmp_path),
    ))
    result = asyncio.run(engine.execute())
    assert result.success
    assert result.stage_results[1].outputs == ["ok"]
    assert result.stage_results[1].artifacts["root"] == str(tmp_path)


def test_generic_workflow_runner_executes_action_without_issue_or_backend(tmp_path: Path):
    from orchestratord.agent.task import AgentTask
    from orchestratord.workflow_engine.actions import ActionResult, register_action
    from orchestratord.workflow_runtime import WorkflowRunner

    register_action("test.no-backend", lambda config, context: ActionResult(success=True, outputs=["done"]))
    workflow = tmp_path / "workflow.yaml"
    workflow.write_text(
        "name: generic\nstages:\n  - id: 1\n    name: task\n    uses: test.no-backend\n",
        encoding="utf-8",
    )
    runner = WorkflowRunner(workflow, task_runner=None, workspace_dir=tmp_path)
    result = asyncio.run(runner.run(AgentTask(id="task-1", kind="training")))
    assert result.success
    assert result.stage_results[1].outputs == ["done"]


def test_action_dag_runs_independent_nodes_concurrently(tmp_path: Path):
    from orchestratord.agent.task import AgentTask
    from orchestratord.workflow_engine.actions import ActionResult, register_action
    from orchestratord.workflow_runtime import WorkflowRunner

    active = 0
    peak = 0

    async def observed(config, context):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.02)
        active -= 1
        return ActionResult(success=True, outputs=[config["name"]])

    register_action("test.concurrent", observed)
    workflow = tmp_path / "parallel.yaml"
    workflow.write_text(
        """name: parallel
stages:
  - {id: 1, name: a, uses: test.concurrent, with: {name: a}}
  - {id: 2, name: b, uses: test.concurrent, with: {name: b}}
  - {id: 3, name: join, uses: test.concurrent, depends_on: [1, 2], with: {name: join}}
""",
        encoding="utf-8",
    )
    runner = WorkflowRunner(
        workflow,
        task_runner=None,
        workspace_dir=tmp_path,
        max_concurrent_stages=2,
    )
    result = asyncio.run(runner.run(AgentTask(id="parallel")))
    assert result.success
    assert peak == 2
    assert result.completed_stages == 3


def test_capability_core_has_no_business_imports():
    from scripts.check_core_boundary import _business_boundary_violations

    repo = Path(__file__).parents[1]
    assert _business_boundary_violations(repo) == []


def test_generic_run_store_round_trip(tmp_path: Path):
    from orchestratord.run_store import RunRecord, RunStore

    store = RunStore(tmp_path)
    store.save(RunRecord("r1", "wf", "t1", "training"))
    record = store.get("r1")
    assert record is not None and record.task_kind == "training"
    assert [item.run_id for item in store.list("running")] == ["r1"]


def test_run_start_persists_action_only_run(tmp_path: Path, capsys):
    from orchestratord.cli.run import _start
    from orchestratord.run_store import RunStore
    from orchestratord.workflow_engine.actions import ActionResult, register_action

    register_action("test.cli", lambda config, context: ActionResult(success=True))
    workflow = tmp_path / "cli.yaml"
    workflow.write_text(
        "name: cli\nstages:\n  - {id: 1, name: execute, uses: test.cli}\n",
        encoding="utf-8",
    )
    args = argparse.Namespace(
        workflow_file=str(workflow), backend=None, runtime_config=None,
        input='{"id":"task-cli","kind":"inference"}', input_file=None,
        workspace=str(tmp_path), max_concurrency=1,
    )
    assert asyncio.run(_start(args)) == 0
    output = capsys.readouterr().out
    assert '"success": true' in output
    records = RunStore(tmp_path).list()
    assert len(records) == 1 and records[0].status == "completed"


def test_generic_run_cancel_is_applied_at_stage_boundary(tmp_path: Path):
    from orchestratord.agent.task import AgentTask
    from orchestratord.workflow_engine.actions import ActionResult, register_action
    from orchestratord.workflow_runtime import WorkflowRunner

    called = False

    def action(config, context):
        nonlocal called
        called = True
        return ActionResult(success=True)

    register_action("test.cancel", action)
    workflow = tmp_path / "cancel.yaml"
    workflow.write_text(
        "name: cancel\nstages:\n  - {id: 1, name: action, uses: test.cancel}\n",
        encoding="utf-8",
    )
    runner = WorkflowRunner(
        workflow,
        task_runner=None,
        workspace_dir=tmp_path,
        control_state=lambda: "cancel_requested",
    )
    result = asyncio.run(runner.run(AgentTask(id="cancel")))
    assert not result.success
    assert result.error == "workflow run cancelled"
    assert not called
