"""G1 CLI 冒烟 — DESIGN_PR_GATE_TEST.md §5.2。

真实执行 console_script：--version、全部子命令 --help、只读命令真实 run()。
子命令清单锁定，双向防漂移（新增 parser 忘注册 / 删模块留死分支）。
"""
from __future__ import annotations

import re

import subprocess
from pathlib import Path

import gate_support as g
import pytest

pytestmark = pytest.mark.gate  # PR merge gate (DESIGN_PR_GATE_TEST.md §4)

EXPECTED_SUBCOMMANDS = (
    Path(__file__).parent / "expected_cli_subcommands.txt"
).read_text(encoding="utf-8").split()

#: 覆盖 argparse 之后真实 run() 分支的只读命令（无副作用、无真实 agent CLI）。
READONLY_COMMANDS = (
    ("backend", "list"),
    ("app", "list"),
)


def _run_cli(args: list[str], **kw: object) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(g.CONSOLE_SCRIPT), *args],
        capture_output=True,
        text=True,
        timeout=60,
        **kw,  # type: ignore[arg-type]
    )


class TestG1:
    def test_version(self) -> None:
        proc = _run_cli(["--version"])
        assert proc.returncode == 0, proc.stderr
        assert "orchestratord" in proc.stdout
        assert "Traceback" not in proc.stderr

    def test_help_registry_lock(self) -> None:
        """--help 输出的子命令集合必须与 expected_cli_subcommands.txt 一致。"""
        proc = _run_cli(["--help"])
        assert proc.returncode == 0, proc.stderr
        m = re.search(r"\{([^}]+)\}", proc.stdout)
        assert m, f"--help 输出中未找到子命令集合:\n{proc.stdout}"
        actual = {c.strip() for c in m.group(1).split(",")}
        expected = set(EXPECTED_SUBCOMMANDS)
        assert actual == expected, (
            "CLI 子命令清单漂移 — "
            f"未登记的新增: {sorted(actual - expected)}; "
            f"已失效的登记: {sorted(expected - actual)}; "
            "请同步更新 tests/gate/expected_cli_subcommands.txt（独立 commit）"
        )

    @pytest.mark.parametrize("sub", EXPECTED_SUBCOMMANDS)
    def test_subcommand_help(self, sub: str) -> None:
        proc = _run_cli([sub, "--help"])
        assert proc.returncode == 0, (
            f"{sub} --help rc={proc.returncode}\n{proc.stderr[-2000:]}"
        )
        assert "Traceback" not in proc.stderr
        assert "usage" in (proc.stdout + proc.stderr).lower()

    @pytest.mark.parametrize("args", READONLY_COMMANDS, ids=lambda a: " ".join(a))
    def test_readonly_real_run(self, args: tuple[str, ...], tmp_path) -> None:
        proc = _run_cli(list(args), cwd=str(tmp_path), env=g.gate_env(tmp_path))
        assert proc.returncode == 0, (
            f"{' '.join(args)} rc={proc.returncode}\n"
            f"{proc.stdout[-2000:]}\n{proc.stderr[-2000:]}"
        )
        assert "Traceback" not in proc.stderr
