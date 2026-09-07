"""§9.3 — ``scripts/agent-cli-command-names.txt`` locks the daemon CLI list.

The lock file is the review-visible contract for "which external agent
CLIs the daemon may execute by default". This module pins it to the
in-core registry (:data:`orchestratord._backend_cli_registry.KNOWN_BACKEND_CLIS`)
that also drives the test-time PATH guard, so a new agent CLI cannot
ship without (a) a registry entry (guard shim) and (b) a lock-file line
(reviewed diff). Always-on and DB-free — no descriptor packages needed.

Additionally, when backend descriptor packages *are* installed, every
descriptor-declared ``cli_command`` must be guard-registered; a CLI the
daemon would spawn must never escape the §9.3 "CI never executes
user-installed agent CLIs" guarantee.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from orchestratord._backend_cli_registry import binary_names, lookup

LOCK_FILE = (
    Path(__file__).resolve().parent.parent / "scripts" / "agent-cli-command-names.txt"
)


def _locked_commands() -> list[str]:
    lines = LOCK_FILE.read_text(encoding="utf-8").splitlines()
    return [line.strip() for line in lines if line.strip()]


def test_lock_file_matches_registry() -> None:
    """Lock file and ``KNOWN_BACKEND_CLIS`` must agree exactly.

    The registry is append-only; when this fails because you just added
    an entry, append the same command to the lock file (and vice versa:
    a lock-file line without a registry entry means the guard never
    intercepts that binary).
    """
    locked = _locked_commands()
    registry = list(binary_names())
    assert sorted(locked) == sorted(registry), (
        f"agent-cli command lock drifted — lock_file_only="
        f"{sorted(set(locked) - set(registry))}, registry_only="
        f"{sorted(set(registry) - set(locked))}. Update both "
        "src/orchestratord/_backend_cli_registry.py and "
        "scripts/agent-cli-command-names.txt together."
    )


def test_lock_file_has_no_duplicates() -> None:
    """A duplicated line would silently mask the real command count."""
    locked = _locked_commands()
    duplicates = sorted({c for c in locked if locked.count(c) > 1})
    assert not duplicates, f"duplicate commands in lock file: {duplicates}"


def test_descriptor_cli_commands_are_guard_registered() -> None:
    """Every descriptor ``cli_command`` must be in the guard registry.

    Skipped when no backend descriptor packages are installed (the
    registry↔lock invariant above still holds unconditionally).
    """
    from orchestratord.backend_registry import discover_descriptors

    descriptors = discover_descriptors()
    if not descriptors:
        pytest.skip("no backend descriptor entry-points installed")
    unregistered = {
        (name, desc.cli_command)
        for name, desc in descriptors.items()
        if desc.cli_command is not None and lookup(desc.cli_command) is None
    }
    assert not unregistered, (
        f"descriptors declare cli_command values the CLI guard does not "
        f"intercept: {sorted(unregistered)}. Register them in "
        "orchestratord._backend_cli_registry (§9.3)."
    )
