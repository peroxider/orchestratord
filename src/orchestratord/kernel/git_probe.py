"""Git probe — 机制侧 git 状态探测（DESIGN §6 kernel 机制域）。

纯 git 管道包装（``git status --porcelain`` / 子进程执行），无业务耦合。
backend_runner 的 outcome 事实码检测（空分支/变更判定，§1.2 C3/C5 与
§4.4）经本模块探测工作区状态；git/utils.py（业务域归属）导入并重导出
以保持既有 import 路径与函数对象同一性。
"""

from __future__ import annotations

import subprocess
from collections import namedtuple
from pathlib import Path

FileStatus = namedtuple("FileStatus", ["path", "status"])


def run_git(args: list[str], cwd: str | Path) -> tuple[str, str, int]:
    """Run a git command and return ``(stdout, stderr, returncode)``."""
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=30,
        )
        return proc.stdout, proc.stderr, proc.returncode
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return "", str(exc), 1


def get_file_status(path: str | Path) -> list[FileStatus]:
    """Return ``[FileStatus(path, status_code), ...]`` from ``git status --porcelain``.

    Returns an empty list on a clean tree, which is falsy.
    """
    out, _, rc = run_git(["status", "--porcelain"], str(path))
    if rc != 0 or not out.strip():
        return []
    result: list[FileStatus] = []
    for line in out.strip().split("\n"):
        if len(line) >= 3:
            result.append(FileStatus(path=line[3:].strip(), status=line[:2].strip()))
    return result
