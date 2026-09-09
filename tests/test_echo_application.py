"""P6 EchoApplication 演练（DESIGN §7 :476-477）。

验收锚点：
- 协议闭环：WorkProvider 产出固定 AgentTask → single mode 执行 →
  interpret_result 记录结果 → Outcome.dispose
- 注册运行：applications 注册表寻址 + ``app list``/CLI 可见
- 零触碰：echo 包不 import issue_pr/orchestrator 任何代码；
  issue_pr/kernel 零改动（以 fresh-interpreter import 探针佐证）
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import subprocess
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

from orchestratord.agent.task import AgentTask, AgentTaskResult
from orchestratord.applications.echo.lifecycle import EchoLifecycle
from orchestratord.applications.echo.provider import EchoWorkProvider
from orchestratord.config.schema import WorkflowConfig
from orchestratord.kernel.application import Outcome
from orchestratord.kernel.run_context import RunContext
from orchestratord.modes.single import SingleModeRunner
from orchestratord.session_state import RunSession, RunSubject


def _run(coro):
    return asyncio.run(coro)


class _EchoStubRunner:
    """确定性执行桩：模拟 single mode 下的 AgentRunner.run。"""

    def __init__(self) -> None:
        self.prompts: list[str] = []

    async def run(self, session, workflow, **hooks):
        self.prompts.append(session.prompt_override or "")
        return AgentTaskResult(
            task_id=session.task.id,
            kind=session.task.kind,
            status="completed",
            output_text=f"ECHO:{session.task.id}",
        )


class TestProtocolClosure(unittest.TestCase):
    """协议闭环演练：poll → prepare_run → single mode → interpret_result。"""

    def test_end_to_end_loop_records_results(self) -> None:
        provider = EchoWorkProvider()
        lifecycle = EchoLifecycle()
        runner = _EchoStubRunner()
        mode = SingleModeRunner(runner)

        items = _run(provider.poll())
        self.assertEqual([i.dedup_key for i in items], ["echo-1", "echo-2", "echo-3"])

        for item in items:
            ctx = RunContext.from_task(item.task)
            prepared = _run(lifecycle.prepare_run(item, ctx))
            self.assertIn("ECHO-", prepared.prompt)
            session = RunSession(
                subject=RunSubject(id=item.dedup_key),
                workspace=SimpleNamespace(path=Path(".")),
                task=item.task,
                prompt_override=prepared.prompt,
            )
            result = _run(mode.run(session, WorkflowConfig()))
            outcome = _run(lifecycle.interpret_result(item, result, ctx))
            self.assertIsInstance(outcome, Outcome)
            self.assertEqual(outcome.kind, "dispose")

        self.assertEqual(
            runner.prompts, [f"Reply with exactly: ECHO-{n}" for n in (1, 2, 3)]
        )
        self.assertEqual(
            lifecycle.records,
            [
                {
                    "dedup_key": f"echo-{n}",
                    "status": "completed",
                    "outcome_code": None,
                    "output": f"ECHO:echo-{n}",
                }
                for n in (1, 2, 3)
            ],
        )
        self.assertEqual(_run(provider.poll()), [], "poll must be exhausted after delivery")

    def test_dispatch_rejected_requeues_item(self) -> None:
        provider = EchoWorkProvider(
            tasks=[AgentTask(id="echo-x", kind="echo", prompt_override="ECHO-X")]
        )
        (item,) = _run(provider.poll())
        _run(provider.on_dispatch_rejected(item, "simulated rate limit"))
        (again,) = _run(provider.poll())
        self.assertEqual(again.dedup_key, "echo-x")

    def test_lifecycle_protocol_surface(self) -> None:
        lifecycle = EchoLifecycle()
        self.assertEqual(lifecycle.name, "echo")
        self.assertEqual(lifecycle.control_commands(), {})
        self.assertEqual(lifecycle.prompt_profiles(), {})
        self.assertIsNone(lifecycle.on_kernel_event(object()))


class TestRegistration(unittest.TestCase):
    """注册运行：注册表寻址 + CLI 可见。"""

    def test_registry_resolves_echo(self) -> None:
        from orchestratord.applications import application_specs, get_application_class
        from orchestratord.applications.echo.app import EchoApplication
        from orchestratord.orchestration_subsystem import OrchestrationSubsystem

        self.assertIs(get_application_class("echo"), EchoApplication)
        self.assertTrue(issubclass(EchoApplication, OrchestrationSubsystem))
        spec = next(s for s in application_specs() if s.name == "echo")
        self.assertEqual(spec.cli_name, "echo")
        self.assertTrue(spec.class_path.endswith("echo.app:EchoApplication"))

    def test_app_list_includes_echo(self) -> None:
        from orchestratord.cli import app as app_cli

        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            rc = app_cli.run(SimpleNamespace(app_subcommand="list"))
        self.assertEqual(rc, 0)
        lines = buffer.getvalue().splitlines()
        self.assertEqual(lines[0].split("\t")[0], "issue-pr")
        self.assertIn("echo", [line.split("\t")[0] for line in lines])

    def test_echo_import_pulls_no_mechanism_business_code(self) -> None:
        """fresh interpreter：import echo lifecycle/provider 不拉
        orchestrator/orchestration_subsystem/issue_pr.app（协议层自足；
        issue_pr.prompts 的级联属既有注册拓扑，不算违例）。"""
        probe = (
            "import sys\n"
            "import orchestratord.applications.echo.provider\n"
            "import orchestratord.applications.echo.lifecycle\n"
            "forbidden = [m for m in sys.modules if m.startswith(\n"
            "    ('orchestratord.orchestrator', 'orchestratord.orchestration_subsystem',\n"
            "     'orchestratord.applications.issue_pr.app'))]\n"
            "assert not forbidden, f'eager pull: {forbidden}'\n"
            "print('echo-lazy-ok')\n"
        )
        proc = subprocess.run(
            [sys.executable, "-c", probe],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, f"probe failed: {proc.stdout}\n{proc.stderr}")
        self.assertIn("echo-lazy-ok", proc.stdout)

    def test_echo_sources_reference_no_issue_pr_code(self) -> None:
        """AST 级钉住：echo 包的 import 语句不含 issue_pr/orchestrator
        模块（验收「不触碰」；docstring 文字不算引用）。"""
        import ast

        pkg = (
            Path(__file__).parent.parent
            / "src"
            / "orchestratord"
            / "applications"
            / "echo"
        )
        for source in sorted(pkg.glob("*.py")):
            tree = ast.parse(source.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    bad = [
                        a.name for a in node.names if "issue_pr" in a.name
                    ] or [
                        a.name
                        for a in node.names
                        if a.name == "orchestratord.orchestrator"
                    ]
                    self.assertFalse(bad, f"{source.name} imports {bad}")
                elif isinstance(node, ast.ImportFrom):
                    module = node.module or ""
                    self.assertNotIn(
                        "issue_pr", module, f"{source.name} imports {module}"
                    )
                    self.assertFalse(
                        module == "orchestratord.orchestrator"
                        or module.startswith("orchestratord.orchestrator."),
                        f"{source.name} imports {module}",
                    )


if __name__ == "__main__":
    unittest.main()
