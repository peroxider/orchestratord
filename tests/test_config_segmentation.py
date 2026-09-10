"""P5 配置与入口分段（DESIGN §4.6、§6 :420/:431-432、P5 :470-472）。

验收锚点：
- 旧 workflow.md 扁平格式加载快照等价（golden fixture，改动前生成）
- kernel:/applications: 双格式加载与旧格式快照等价
- 旧格式加载打 deprecation 日志（DESIGN :336）
- WorkflowConfig.kernel / .issue_pr 组合壳视图（B5 接缝，:155）
- applications 注册表惰性寻址（:420）+ cli/app、cli/server 经注册表
  装配（:431）
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import logging
import subprocess
import sys
import unittest
from dataclasses import asdict, fields
from pathlib import Path
from unittest.mock import patch

FIXTURES = Path(__file__).parent / "fixtures"


def _load_json(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _snapshot(config) -> dict:
    """asdict → JSON round-trip（tuple 归一为 list，与 golden 同构）。"""
    return json.loads(json.dumps(asdict(config)))


class _RecordCollector(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


# ── 1. 双格式加载与快照等价（P5 验收） ────────────────────────────────


class TestLegacyDualFormatLoading(unittest.TestCase):
    """旧扁平格式快照等价 + kernel:/applications: 双格式等价。"""

    def setUp(self) -> None:
        from orchestratord.config.schema import WorkflowConfig

        self._WorkflowConfig = WorkflowConfig
        self._handler = _RecordCollector()
        logging.getLogger("orchestratord.config.schema").addHandler(self._handler)

    def tearDown(self) -> None:
        logging.getLogger("orchestratord.config.schema").removeHandler(self._handler)

    def test_legacy_snapshot_equivalent(self) -> None:
        """旧扁平格式加载与改动前的 golden 快照逐字段等价。"""
        legacy = self._WorkflowConfig.from_dict(_load_json("p5_legacy_config_input.json"))
        self.assertEqual(_snapshot(legacy), _load_json("p5_legacy_config_snapshot.json"))

    def test_dual_format_equivalent(self) -> None:
        """kernel:/applications: 双格式加载结果与旧格式完全等价。"""
        legacy = self._WorkflowConfig.from_dict(_load_json("p5_legacy_config_input.json"))
        dual = self._WorkflowConfig.from_dict(_load_json("p5_dual_config_input.json"))
        self.assertEqual(_snapshot(dual), _snapshot(legacy))

    def test_legacy_load_logs_deprecation(self) -> None:
        """旧格式加载打 deprecation 日志（DESIGN :336）；新格式不打。"""
        self._WorkflowConfig.from_dict(_load_json("p5_legacy_config_input.json"))
        deprecations = [
            m for m in self._handler.messages if "legacy flat layout" in m
        ]
        self.assertEqual(len(deprecations), 1)

        self._handler.messages.clear()
        self._WorkflowConfig.from_dict(_load_json("p5_dual_config_input.json"))
        self.assertEqual(
            [m for m in self._handler.messages if "legacy flat layout" in m],
            [],
        )

    def test_dual_format_section_collision_nested_wins(self) -> None:
        """同名段同时出现在扁平与新格式位置：新格式优先（warn）。"""
        raw = _load_json("p5_dual_config_input.json")
        raw["polling"] = {"interval_ms": 1}  # 扁平残留
        config = self._WorkflowConfig.from_dict(raw)
        self.assertEqual(config.polling.interval_ms, 12345)

    def test_malformed_dual_format_rejected(self) -> None:
        """applications:/kernel: 类型错误必须显式失败，不得静默降级。"""
        with self.assertRaises(TypeError):
            self._WorkflowConfig.from_dict({"applications": ["issue_pr"]})
        with self.assertRaises(TypeError):
            self._WorkflowConfig.from_dict(
                {"applications": {"issue_pr": {}}, "kernel": "oops"}
            )


# ── 2. 组合壳视图（B5 接缝） ──────────────────────────────────────────


class TestConfigSegmentViews(unittest.TestCase):
    """WorkflowConfig.kernel / .issue_pr 组合壳：引用语义 + 属性面保持。"""

    EXPECTED_FIELDS = (
        "tracker",
        "polling",
        "workspace",
        "worker",
        "agent",
        "sandbox",
        "hooks",
        "review_feedback",
        "rules",
        "experience",
        "telemetry",
        "observability",
        "server",
        "modes",
        "pr_template",
        "pr_conflict_scan",
        "clarifier",
        "source_path",
    )

    def test_dataclass_field_surface_unchanged(self) -> None:
        """壳以 property 暴露，不得进入 dataclass 字段面（构造/asdict 兼容）。"""
        from orchestratord.config.schema import WorkflowConfig

        self.assertEqual(
            tuple(f.name for f in fields(WorkflowConfig)), self.EXPECTED_FIELDS
        )
        self.assertNotIn("kernel", asdict(WorkflowConfig()))
        self.assertNotIn("issue_pr", asdict(WorkflowConfig()))

    def test_kernel_view_reference_semantics(self) -> None:
        from orchestratord.config.schema import WorkflowConfig

        config = WorkflowConfig.from_dict({"polling": {"interval_ms": 777}})
        view = config.kernel
        self.assertIs(view.modes, config.modes)
        self.assertIs(view.sandbox, config.sandbox)
        self.assertEqual(view.polling.interval_ms, 777)
        for name in (
            "polling",
            "worker",
            "sandbox",
            "modes",
            "observability",
            "server",
            "agent",
        ):
            self.assertTrue(hasattr(view, name), f"KernelSettings.{name} missing")

    def test_issue_pr_view_reference_semantics(self) -> None:
        from orchestratord.config.schema import WorkflowConfig

        config = WorkflowConfig.from_dict({"clarifier": {"max_questions": 2}})
        view = config.issue_pr
        self.assertIs(view.tracker, config.tracker)
        self.assertIs(view.clarifier, config.clarifier)
        self.assertEqual(view.clarifier.max_questions, 2)
        for name in (
            "tracker",
            "workspace",
            "agent",
            "hooks",
            "review_feedback",
            "rules",
            "telemetry",
            "pr_template",
            "pr_conflict_scan",
            "clarifier",
        ):
            self.assertTrue(hasattr(view, name), f"IssuePrSettings.{name} missing")

    def test_agent_instance_shared_between_views(self) -> None:
        from orchestratord.config.schema import WorkflowConfig

        config = WorkflowConfig()
        self.assertIs(config.kernel.agent, config.agent)
        self.assertIs(config.kernel.agent, config.issue_pr.agent)

    def test_view_tracks_section_reassignment(self) -> None:
        """宿主事后替换嵌套段（backend_runner.run_task 模式）必须可见。"""
        from orchestratord.config.schema import AgentConfig, WorkflowConfig

        config = WorkflowConfig.from_dict({"agent": {"model": "original"}})
        config.agent = AgentConfig(model="reassigned")
        self.assertEqual(config.kernel.agent.model, "reassigned")
        self.assertIs(config.issue_pr.agent, config.agent)


# ── 3. applications 注册表（DESIGN §6 :420） ──────────────────────────


class TestApplicationRegistry(unittest.TestCase):
    """name → Application 组合根类的注册表寻址与惰性绑定。"""

    def test_get_application_class_identity(self) -> None:
        from orchestratord.applications import (
            IssueToPrApplication,
            get_application_class,
        )
        from orchestratord.orchestration_subsystem import OrchestrationSubsystem

        resolved = get_application_class("issue_pr")
        self.assertIs(resolved, IssueToPrApplication)
        self.assertTrue(issubclass(resolved, OrchestrationSubsystem))

    def test_unknown_name_raises_keyerror(self) -> None:
        from orchestratord.applications import get_application_class

        with self.assertRaises(KeyError) as ctx:
            get_application_class("nope")
        self.assertIn("issue_pr", str(ctx.exception))

    def test_specs_entry(self) -> None:
        from orchestratord.applications import application_specs

        spec = next(s for s in application_specs() if s.name == "issue_pr")
        self.assertEqual(spec.cli_name, "issue-pr")
        self.assertTrue(spec.class_path.endswith("app:IssueToPrApplication"))

    def test_registry_binding_is_lazy(self) -> None:
        """注册表 import 不触发 orchestration_subsystem / app 绑定
        （import 时序契约）；get_application_class 调用才绑定。"""
        probe = (
            "import sys\n"
            "import orchestratord.applications as apps\n"
            "eager = [m for m in (\n"
            "    'orchestratord.orchestration_subsystem',\n"
            "    'orchestratord.applications.issue_pr.app',\n"
            ") if m in sys.modules]\n"
            "assert not eager, f'eager import: {eager}'\n"
            "cls = apps.get_application_class('issue_pr')\n"
            "assert cls.__name__ == 'IssueToPrApplication'\n"
            "assert 'orchestratord.orchestration_subsystem' in sys.modules\n"
            "print('registry-lazy-ok')\n"
        )
        proc = subprocess.run(
            [sys.executable, "-c", probe],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        self.assertEqual(
            proc.returncode, 0, f"probe failed: {proc.stdout}\n{proc.stderr}"
        )
        self.assertIn("registry-lazy-ok", proc.stdout)


# ── 4. CLI 入口经注册表寻址（DESIGN §6 :431） ─────────────────────────


class TestCliRegistryAddressing(unittest.TestCase):
    """``app`` 命令组与 daemon 装配按注册表寻址组合根类。"""

    def test_app_list_is_registry_driven(self) -> None:
        from orchestratord.cli import app as app_cli

        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            rc = app_cli.run(argparse.Namespace(app_subcommand="list"))
        self.assertEqual(rc, 0)
        # P6 起注册表含 echo 演练条目：issue-pr 行逐字不变 + echo 行存在。
        lines = buffer.getvalue().splitlines()
        self.assertEqual(
            lines[0],
            (
                "issue-pr\tPoll tracker issues, run coding workflows, "
                "and synchronize pull requests"
            ),
        )
        self.assertIn("echo\t", lines[1])

    def test_server_parser_threads_application_default(self) -> None:
        """canonical 与 app 委托两条入口线均把注册名带到 args.application。"""
        from orchestratord.cli.server import add_server_parser

        parser = argparse.ArgumentParser()
        subs = parser.add_subparsers(dest="server_subcommand")
        add_server_parser(subs)
        args = parser.parse_args(["server", "start", "--backend", "test"])
        self.assertEqual(args.application, "issue_pr")

        parser = argparse.ArgumentParser()
        subs = parser.add_subparsers(dest="top")
        add_server_parser(
            subs, command_name="issue-pr", start_command="serve", application="issue_pr"
        )
        args = parser.parse_args(["issue-pr", "serve", "--backend", "test"])
        self.assertEqual(args.application, "issue_pr")

    def test_resolve_application_class_goes_through_registry(self) -> None:
        from orchestratord.applications import IssueToPrApplication
        from orchestratord.cli.server import _resolve_application_class

        self.assertIs(_resolve_application_class("issue_pr"), IssueToPrApplication)

        sentinel = object()
        with patch(
            "orchestratord.applications.get_application_class",
            return_value=sentinel,
        ):
            self.assertIs(_resolve_application_class("issue_pr"), sentinel)

    def test_server_start_does_not_hardcode_application_class(self) -> None:
        """入口直连业务组合根类属 :431 违例——源级钉住注册表寻址接缝。"""
        source = (
            Path(__file__).parent.parent
            / "src"
            / "orchestratord"
            / "cli"
            / "server.py"
        ).read_text(encoding="utf-8")
        self.assertNotIn("IssueToPrApplication", source)
        self.assertIn("_resolve_application_class(", source)


if __name__ == "__main__":
    unittest.main()
