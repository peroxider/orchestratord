"""Verification gate contract for an explicit ``agent.test_command``.

Contract (restored after a rejected alternative design): an explicit
``agent.test_command`` is ALWAYS a hard gate — non-zero exit blocks the
push, with no baseline exemption and no dependence on
``regression_guard``. A workspace with knowingly-red baseline tests must
exclude them in the command itself (``--deselect`` / ``--ignore``),
which keeps the gate predictable: what the operator wrote is exactly
what runs, and red means red.

The baseline-comparison path exists only for the AUTO-DETECTED fallback
suite (no ``test_command`` configured) — see ``_run_regression_guard``.

Observed live on orchestratord issue #14: the clawcodex-ascend suite
has one pre-existing baseline failure; the operator remedy is a
``--deselect`` in the workflow's ``test_command``, not implicit
gate-side waving.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

from orchestratord.config.schema import AgentConfig, VerificationConfig
from orchestratord.git.sync import GitSyncService, VerificationFailed

PY = sys.executable
CMD = f'"{PY}" -m pytest tests -q --tb=no'


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True
    )
    return result.stdout.strip()


def _init_repo_with_red_baseline(tmp_path: Path) -> tuple[Path, str]:
    """A repo whose sole test fails at the START commit (red baseline)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    tests = repo / "tests"
    tests.mkdir()
    (tests / "test_x.py").write_text("def test_always_red():\n    assert False\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "baseline with a red test")
    return repo, _git(repo, "rev-parse", "HEAD")


class _Session:
    """Minimal session surface consumed by verification."""

    def __init__(self, start_sha: str) -> None:
        self.start_commit_sha = start_sha
        self.verification_status: str | None = None
        self.verification_output: str | None = None


def _service(test_command: str, regression_guard: bool) -> GitSyncService:
    agent_cfg = AgentConfig(
        test_command=test_command,
        verification=VerificationConfig(regression_guard=regression_guard, timeout_ms=120_000),
    )
    return GitSyncService(tracker=Mock(), agent_config=agent_cfg)


@pytest.mark.asyncio
async def test_explicit_command_blocks_red_suite_even_with_baseline_failures(
    tmp_path: Path,
) -> None:
    """Regression guard ON does NOT soften an explicit test_command: a
    red suite blocks even when every failure is pre-existing at the
    start commit. The remedy is exclusion in the command, not here."""
    repo, start_sha = _init_repo_with_red_baseline(tmp_path)
    (repo / "tests" / "test_new.py").write_text("def test_new():\n    assert True\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "feature change")

    service = _service(CMD, regression_guard=True)
    with pytest.raises(VerificationFailed) as raised:
        await service._run_pre_push_verification(str(repo), _Session(start_sha))
    assert "test verification failed" in str(raised.value)


@pytest.mark.asyncio
async def test_explicit_command_deselect_of_baseline_failure_passes(tmp_path: Path) -> None:
    """The operator remedy: deselect the known-red test in the command —
    the gate then passes on the green remainder."""
    repo, start_sha = _init_repo_with_red_baseline(tmp_path)
    (repo / "tests" / "test_new.py").write_text("def test_new():\n    assert True\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "feature change")

    deselect = "--deselect tests/test_x.py::test_always_red"
    service = _service(f'"{PY}" -m pytest tests -q --tb=no {deselect}', regression_guard=True)
    session = _Session(start_sha)
    await service._run_pre_push_verification(str(repo), session)
    assert session.verification_status == "passed"


@pytest.mark.asyncio
async def test_explicit_command_guard_off_red_suite_blocks(tmp_path: Path) -> None:
    """regression_guard: false changes nothing for an explicit command —
    the hard gate does not depend on the flag."""
    repo, start_sha = _init_repo_with_red_baseline(tmp_path)

    service = _service(CMD, regression_guard=False)
    with pytest.raises(VerificationFailed) as raised:
        await service._run_pre_push_verification(str(repo), _Session(start_sha))
    assert "test verification failed" in str(raised.value)


@pytest.mark.asyncio
async def test_explicit_command_green_suite_passes(tmp_path: Path) -> None:
    repo, start_sha = _init_repo_with_red_baseline(tmp_path)
    (repo / "tests" / "test_x.py").write_text("def test_fixed():\n    assert True\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "fixes the baseline failure")

    service = _service(CMD, regression_guard=True)
    session = _Session(start_sha)
    await service._run_pre_push_verification(str(repo), session)
    assert session.verification_status == "passed"
