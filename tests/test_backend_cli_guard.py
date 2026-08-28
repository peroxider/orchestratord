"""Tests for the backend-CLI PATH guard (DESIGN_backend_cli_test_guard.md §7).

These tests pin the behavior of the autouse fixture in
``tests/conftest.py``:

1. Subprocess invocations of any guarded binary fail with exit 126
   and the ``[backend-cli-guard]`` sentinel on stderr.
2. The same is true for every binary registered in
   ``orchestratord._backend_cli_registry.KNOWN_BACKEND_CLIS``.
3. A test marked with ``@pytest.mark.uses_real_cli`` is exempt.
4. Tests under ``tests/manual_e2e_`` are exempt by path.
5. Setting ``ORCHESTRATORD_SKIP_CLI_GUARD=1`` disables the guard
   entirely (CI escape hatch).

Most tests do NOT rely on the autouse fixture: they call
``install_cli_shims(parent=...)`` themselves and prepend PATH for a
single subprocess call. This keeps the test self-contained — the
fixture's behavior is verified separately by reading the conftest
source for the literal contract strings.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from orchestratord._backend_cli_registry import KNOWN_BACKEND_CLIS, binary_names, lookup
from scripts.install_cli_shims import install_cli_shims


@pytest.fixture
def tmp_dir(tmp_path: Path) -> Path:
    return tmp_path


def _run_guarded(binary: str, *, guard_dir: Path) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["PATH"] = f"{guard_dir}{os.pathsep}{env.get('PATH', '')}"
    executable = f"{binary}.cmd" if sys.platform == "win32" else binary
    return subprocess.run(
        [executable, "--help"],
        capture_output=True,
        env=env,
        timeout=30,
    )


def test_guard_blocks_invocation(tmp_dir: Path) -> None:
    """A single guarded binary fails with exit 126 and the sentinel."""
    guard_dir = install_cli_shims(parent=tmp_dir / "guard")
    result = _run_guarded("clawcodex-dev", guard_dir=guard_dir)
    assert result.returncode == 126, (
        f"expected 126, got {result.returncode}; stderr={result.stderr!r}"
    )
    assert b"[backend-cli-guard]" in result.stderr
    assert b"'clawcodex-dev'" in result.stderr
    assert b"orchestratord-clawcodex" in result.stderr


def test_guard_blocks_all_known_binaries(tmp_dir: Path) -> None:
    """Every binary in KNOWN_BACKEND_CLIS is intercepted."""
    guard_dir = install_cli_shims(parent=tmp_dir / "guard")
    names = binary_names()
    assert len(names) == len(KNOWN_BACKEND_CLIS) >= 5
    for name in names:
        result = _run_guarded(name, guard_dir=guard_dir)
        assert result.returncode == 126, (
            f"{name!r} should have been blocked; rc={result.returncode}"
        )
        assert b"[backend-cli-guard]" in result.stderr
        assert name.encode() in result.stderr


def test_registry_round_trip() -> None:
    """``binary_names()`` / ``lookup()`` agree with ``KNOWN_BACKEND_CLIS``."""
    for cli in KNOWN_BACKEND_CLIS:
        assert lookup(cli.binary) is cli, f"lookup({cli.binary!r}) round-trip failed"
    assert lookup("nonexistent-binary-xyz") is None


def test_registry_no_duplicates() -> None:
    """No two entries share the same binary name (would shadow each other)."""
    seen: set[str] = set()
    for cli in KNOWN_BACKEND_CLIS:
        assert cli.binary not in seen, f"duplicate binary: {cli.binary!r}"
        seen.add(cli.binary)


def test_install_cli_shims_creates_expected_files(tmp_dir: Path) -> None:
    """``install_cli_shims(parent=...)`` writes the expected layout."""
    guard_dir = install_cli_shims(parent=tmp_dir / "guard")
    assert (guard_dir / "_shim_runner.py").exists()
    for cli in KNOWN_BACKEND_CLIS:
        shim = guard_dir / cli.binary
        assert shim.exists(), f"missing shim: {shim}"
        mode = shim.stat().st_mode
        if sys.platform == "win32":
            assert (guard_dir / f"{cli.binary}.cmd").exists()
        else:
            assert mode & 0o100, f"shim not executable: {shim} (mode={oct(mode)})"
        body = shim.read_text()
        assert "ORCHESTRATORD_GUARDED_BINARY" in body
        assert cli.binary in body
        assert "from _shim_runner import main" in body


def test_shim_runner_returns_126() -> None:
    """The shared runner exits 126 and prints the sentinel."""
    shim_runner = (
        Path(__file__).resolve().parent.parent / "scripts" / "_cli_shims" / "_shim_runner.py"
    )
    proc = subprocess.run(
        [sys.executable, str(shim_runner)],
        capture_output=True,
        env={
            **os.environ,
            "ORCHESTRATORD_GUARDED_BINARY": "codex",
            "ORCHESTRATORD_GUARDED_BACKEND_PKG": "orchestratord-codex",
        },
        timeout=10,
    )
    assert proc.returncode == 126
    assert b"[backend-cli-guard]" in proc.stderr
    assert b"'codex'" in proc.stderr
    assert b"orchestratord-codex" in proc.stderr


# ----- conftest contract pinning ----------------------------------------
#
# These two tests do NOT shell out to anything. They read conftest.py
# as text and assert that the documented exemption hooks are still
# wired up. If someone refactors ``_backend_cli_guard_path`` and drops
# either branch, these tests fail loudly.


def test_manual_e2e_files_exempt_by_path() -> None:
    """Tests under ``tests/manual_e2e_`` are exempt by nodeid prefix."""
    conftest_src = (Path(__file__).resolve().parent / "conftest.py").read_text(encoding="utf-8")
    assert 'nodeid.startswith("tests/manual_e2e_")' in conftest_src, (
        "conftest.py must retain the manual_e2e_ path-prefix exemption"
    )


def test_skip_env_var_disables_guard() -> None:
    """``ORCHESTRATORD_SKIP_CLI_GUARD=1`` disables the autouse fixture."""
    conftest_src = (Path(__file__).resolve().parent / "conftest.py").read_text(encoding="utf-8")
    assert "ORCHESTRATORD_SKIP_CLI_GUARD" in conftest_src
    assert '== "1"' in conftest_src, (
        "guard skip-env check must compare to literal '1' for safety"
    )


def test_uses_real_cli_marker_registered() -> None:
    """``pytest.mark.uses_real_cli`` is registered (no ``PytestUnknownMarkWarning``)."""
    conftest_src = (Path(__file__).resolve().parent / "conftest.py").read_text(encoding="utf-8")
    assert "uses_real_cli" in conftest_src
    # ``pytest_configure`` calls ``addinivalue_line("markers", ...)``.
    assert "addinivalue_line" in conftest_src
    assert "uses_real_cli" in conftest_src.split("addinivalue_line")[1].split(")")[0]
