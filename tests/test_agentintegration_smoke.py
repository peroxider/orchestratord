"""§9.3 agentintegration smoke — REAL user-installed agent CLIs.

Pytest analogue of multica's ``agentintegration`` build tag: every test
here drives an actual agent CLI binary found on ``$PATH``. It is
**deselected by default** via ``pyproject.toml`` ``addopts =
"-m 'not agentintegration'"`` so CI never resolves/executes
user-installed CLIs.

Opt in on a developer machine::

    ORCHESTRATORD_SKIP_CLI_GUARD=1 uv run pytest -m agentintegration

``uses_real_cli`` (also applied below) lifts the conftest PATH guard;
``ORCHESTRATORD_SKIP_CLI_GUARD=1`` is still recommended so non-marked
helpers are not shim-blocked either. The binary list is locked by
``scripts/agent-cli-command-names.txt`` (see
``tests/test_agent_cli_command_lock.py``).
"""

from __future__ import annotations

import shutil
import subprocess

import pytest

from orchestratord._backend_cli_registry import binary_names

pytestmark = [
    pytest.mark.agentintegration,
    pytest.mark.uses_real_cli,
]


@pytest.mark.parametrize("binary", binary_names())
def test_real_cli_responds_to_help_probe(binary: str) -> None:
    """The installed binary launches and answers a ``--help`` probe.

    Deliberately shallow — full wire-format sessions belong to each
    backend package's own tests (fake CLIs) and ``tests/manual_e2e_*``
    (credential-bearing). Here we only assert the binary exists, is
    executable, and prints something, i.e. a user install is not
    fundamentally broken.
    """
    if shutil.which(binary) is None:
        pytest.skip(f"{binary!r} is not installed on this machine")

    proc = subprocess.run(
        [binary, "--help"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode != 127, f"{binary!r} not found despite which()"
    assert proc.returncode != 126, (
        f"{binary!r} was blocked by the CLI guard — this module must run "
        "with uses_real_cli/agentintegration exemptions active"
    )
    assert (proc.stdout + proc.stderr).strip(), (
        f"{binary!r} --help produced no output (rc={proc.returncode})"
    )
