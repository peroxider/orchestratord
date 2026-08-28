"""Git utility functions — self-contained.

Replaces the ``_run_git`` / ``get_repo_root`` / ``get_default_branch`` /
``get_file_status`` symbols that were previously imported from
the backend-neutral git helpers in this module.
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


def get_current_branch(path: str | Path) -> str | None:
    """Return the current branch name, or None if not in a git repo."""
    out, _, rc = run_git(["rev-parse", "--abbrev-ref", "HEAD"], str(path))
    if rc == 0 and out.strip() and out.strip() != "HEAD":
        return out.strip()
    return None
