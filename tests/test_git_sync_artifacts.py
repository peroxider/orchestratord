"""Regression tests for :class:`GitSyncService` artifact management.

The :meth:`GitSyncService._unstage_orchestrator_artifacts` safety net
must catch every type of runtime artifact the orchestrator writes into
the workspace — including the ``.run_control/`` directory (sockets and
endpoint transcripts) that was previously missing from the artifact
list, allowing a run-control file to reach a PR commit.
"""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path
from unittest.mock import Mock

from orchestratord.git.sync import GitSyncService


def _init_bare_repo(root: str) -> str:
    """Initialise a git repo in *root* and return its path."""
    subprocess.run(["git", "init", "-q", root], check=True)
    subprocess.run(
        ["git", "-C", root, "config", "user.email", "test@test.invalid"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", root, "config", "user.name", "Test"],
        check=True,
    )
    return root


def test_unstage_removes_run_control_artifact() -> None:
    """:meth:`_unstage_orchestrator_artifacts` must unstage
    ``.run_control/*`` files so they never enter a commit.

    Regression test for PR #38: a ``.run_control/`` endpoint transcript
    was committed as part of an implementation change because the
    directory was not listed in ``_ORCHESTRATOR_ARTIFACTS``.
    """
    with tempfile.TemporaryDirectory() as tmp:
        _init_bare_repo(tmp)

        # Create a .run_control artifact (simulating a live-run socket
        # endpoint transcript) and stage it as if the agent did a
        # ``git add -A``.
        control_dir = Path(tmp) / ".run_control"
        control_dir.mkdir()
        (control_dir / "20260909_120621_38-deadbeef.endpoint.json").write_text(
            '{"endpoint": "/tmp/test.sock", "pausable": false}\n',
            encoding="utf-8",
        )
        subprocess.run(["git", "-C", tmp, "add", "."], check=True)

        # Before the fix, diff --cached would show the file — confirm
        # the staged file exists.
        staged_before = subprocess.run(
            ["git", "-C", tmp, "diff", "--cached", "--name-only"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        assert ".run_control/" in staged_before, (
            "Precondition: .run_control file should be staged"
        )

        # Act: run the safety net
        service = GitSyncService(tracker=Mock())
        service._unstage_orchestrator_artifacts(tmp)

        # Assert: the .run_control file is no longer staged
        staged_after = subprocess.run(
            ["git", "-C", tmp, "diff", "--cached", "--name-only"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        assert ".run_control" not in staged_after, (
            "_unstage_orchestrator_artifacts should have unstaged "
            ".run_control/ files"
        )
        assert staged_after == "", (
            f"Expected empty staging area, got: {staged_after}"
        )


def test_unstage_keeps_non_artifact_file_staged() -> None:
    """Regular source files should remain staged after artifact cleanup."""
    with tempfile.TemporaryDirectory() as tmp:
        _init_bare_repo(tmp)

        # Create a normal source file and a .run_control artifact
        (Path(tmp) / "src" / "main.py").parent.mkdir(parents=True)
        (Path(tmp) / "src" / "main.py").write_text(
            "def main(): pass\n", encoding="utf-8"
        )
        (Path(tmp) / ".run_control").mkdir()
        (Path(tmp) / ".run_control" / "endpoint.json").write_text(
            "{}", encoding="utf-8"
        )

        subprocess.run(["git", "-C", tmp, "add", "."], check=True)

        service = GitSyncService(tracker=Mock())
        service._unstage_orchestrator_artifacts(tmp)

        staged = subprocess.run(
            ["git", "-C", tmp, "diff", "--cached", "--name-only"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()

        # The .run_control artifact should be unstaged, but src/main.py
        # should remain.
        assert ".run_control" not in staged
        assert "src/main.py" in staged