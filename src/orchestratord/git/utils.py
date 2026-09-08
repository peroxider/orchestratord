"""Git utility functions — self-contained.

Replaces the ``_run_git`` / ``get_repo_root`` / ``get_default_branch`` /
``get_file_status`` symbols that were previously imported from
the backend-neutral git helpers in this module.
"""

from __future__ import annotations

from pathlib import Path

# run_git / FileStatus / get_file_status 定义归属机制域
# （kernel/git_probe.py，DESIGN §6）；此处导入并重导出，既有 import
# 路径与函数对象同一性不变。
from ..kernel.git_probe import FileStatus, get_file_status, run_git

__all__ = [
    "FileStatus",
    "get_current_branch",
    "get_default_branch",
    "get_file_status",
    "get_repo_root",
    "run_git",
]


def get_repo_root(path: str | Path) -> str | None:
    """Return the absolute path to the git repo root, or None."""
    out, _, rc = run_git(["rev-parse", "--show-toplevel"], str(path))
    if rc == 0 and out.strip():
        return out.strip()
    return None


def get_default_branch(path: str | Path) -> str:
    """Return the default branch name (e.g. ``main``), falling back to ``main``."""
    out, _, rc = run_git(
        ["rev-parse", "--abbrev-ref", "origin/HEAD"],
        str(path),
    )
    if rc == 0 and out.strip():
        branch = out.strip()
        if "/" in branch:
            return branch.split("/", 1)[1]
        return branch
    for candidate in ("main", "master"):
        _, _, rc = run_git(["rev-parse", "--verify", candidate], str(path))
        if rc == 0:
            return candidate
    return "main"


def get_current_branch(path: str | Path) -> str | None:
    """Return the current branch name, or None if not in a git repo."""
    out, _, rc = run_git(["rev-parse", "--abbrev-ref", "HEAD"], str(path))
    if rc == 0 and out.strip() and out.strip() != "HEAD":
        return out.strip()
    return None
