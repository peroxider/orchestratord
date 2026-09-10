"""G0 静态健全 — DESIGN_PR_GATE_TEST.md §5.1。

防"import 即崩"：全模块强制导入 + compileall + lint + 版本可解析。
"""
from __future__ import annotations

import subprocess

import importlib
import sys
from pathlib import Path

import gate_support as g
import pytest

pytestmark = pytest.mark.gate  # PR merge gate (DESIGN_PR_GATE_TEST.md §4)


def _src_pkg_root() -> Path:
    return g.REPO_ROOT / "src" / "orchestratord"


def _iter_module_names():
    root = _src_pkg_root()
    for p in sorted(root.rglob("*.py")):
        rel = p.relative_to(root.parent)  # 相对 src/
        if p.name == "__init__.py":
            parts = rel.parts[:-1]
        else:
            parts = rel.with_suffix("").parts
        if parts:
            yield ".".join(parts)


class TestG0:
    def test_compileall(self) -> None:
        proc = subprocess.run(
            [sys.executable, "-m", "compileall", "-q", "src", "backends"],
            cwd=str(g.REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=180,
        )
        assert proc.returncode == 0, (
            f"compileall failed:\n{proc.stdout[-4000:]}\n{proc.stderr[-2000:]}"
        )

    def test_import_every_module(self) -> None:
        """懒加载 dispatch（cli/main.py）在单元测试中永不触发——门禁全量触发。"""
        failures: list[str] = []
        for mod in _iter_module_names():
            try:
                importlib.import_module(mod)
            except Exception as exc:  # noqa: BLE001
                failures.append(f"{mod}: {type(exc).__name__}: {exc}")
        assert not failures, "import 断裂（模块导入即崩）:\n" + "\n".join(failures)

    def test_ruff_clean(self, request: pytest.FixtureRequest) -> None:
        ruff = Path(sys.executable).parent / "ruff"
        g.registered_skip(
            request,
            "G0.ruff",
            env_ok=ruff.exists(),
            env_gone="ruff 未安装于当前 venv",
        )
        proc = subprocess.run(
            # P0 规则集只启用致命类（E9 语法错误）。存量 963 条违规
            # （F401/I001/F821 前向引用等）待专项清理后按季度收紧规则集
            # （DESIGN_PR_GATE_TEST.md §5.1）。
            [str(ruff), "check", "src", "backends", "--select", "E9"],
            cwd=str(g.REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=180,
        )
        assert proc.returncode == 0, f"ruff 违规:\n{proc.stdout[-6000:]}"
