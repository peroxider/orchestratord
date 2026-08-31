from __future__ import annotations

import argparse
import asyncio
from pathlib import Path


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
