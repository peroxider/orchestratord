"""Static dependency boundary check for the orchestration core."""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path


def _python_files(root: Path) -> list[Path]:
    return sorted(root.rglob("*.py"))


def _violations(root: Path) -> list[str]:
    result: list[str] = []
    for path in _python_files(root):
        source = path.read_text(encoding="utf-8")
        if re.search(r"^\s*from\s+\.\.api(?:\.|\s|$)", source, re.MULTILINE):
            result.append(f"{path}: relative API import")
        if re.search(r"extensions(?:\.|\s)", source):
            result.append(f"{path}: forbidden runtime name")
        try:
            tree = ast.parse(source, filename=str(path))
        except SyntaxError as exc:
            result.append(f"{path}: syntax error: {exc}")
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                if node.module.startswith("extensions"):
                    result.append(f"{path}:{node.lineno}: direct runtime import")
    return result


def _business_boundary_violations(repo: Path) -> list[str]:
    """Prevent orchestration primitives from importing business packages."""
    targets = [
        repo / "src" / "orchestratord" / "agent",
        repo / "src" / "orchestratord" / "workflow_engine",
        repo / "src" / "orchestratord" / "backend_runner.py",
        repo / "src" / "orchestratord" / "session_state.py",
        repo / "src" / "orchestratord" / "workflow_runtime.py",
        repo / "src" / "orchestratord" / "run_store.py",
    ]
    forbidden = (
        "issue_registry",
        "repo_tracker",
        "local_tracker",
        "linear",
        "orchestration_subsystem",
        "orchestrator",
    )
    violations: list[str] = []
    files: list[Path] = []
    for target in targets:
        files.extend(_python_files(target) if target.is_dir() else [target])
    for path in files:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            module = ""
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
            elif isinstance(node, ast.Import):
                module = ",".join(alias.name for alias in node.names)
            if any(part in module.split(".") for part in forbidden):
                violations.append(f"{path}:{node.lineno}: business import {module}")
    return violations


def main() -> int:
    repo = Path(__file__).resolve().parents[1]
    violations = _violations(repo / "src" / "orchestratord")
    violations += _violations(repo / "tests")
    violations += _business_boundary_violations(repo)
    pyproject = (repo / "pyproject.toml").read_text(encoding="utf-8")
    if "/mnt/c/WorkSpace/" in pyproject:
        violations.append("pyproject.toml: absolute developer-machine path")
    if violations:
        print("Core boundary violations:")
        print("\n".join(f"- {item}" for item in violations))
        return 1
    print("Core boundary check passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
