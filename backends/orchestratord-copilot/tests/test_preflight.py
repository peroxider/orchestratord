"""Copilot backend preflight + capability smoke tests."""

from __future__ import annotations

import shutil
from dataclasses import fields

import pytest

from orchestratord.spi.backend import SessionSpec
from orchestratord_copilot.backend import CopilotBackend
from orchestratord_copilot.descriptor import COPILOT_DESCRIPTOR


def test_descriptor_capabilities_match_backend() -> None:
    """Descriptor bit set must equal backend.capabilities() output."""
    backend = CopilotBackend()
    descriptor_bits = set(COPILOT_DESCRIPTOR.capabilities)
    backend_bits = {
        f.name
        for f in fields(backend.capabilities())
        if getattr(backend.capabilities(), f.name)
    }
    assert backend_bits == descriptor_bits, (
        f"copilot: backend bits {backend_bits} drifted from descriptor "
        f"{descriptor_bits}"
    )


def test_descriptor_metadata() -> None:
    """Descriptor identifies the package and CLI surface correctly."""
    assert COPILOT_DESCRIPTOR.name == "copilot"
    assert COPILOT_DESCRIPTOR.family.value == "Cli"
    assert COPILOT_DESCRIPTOR.backend_package == "orchestratord-copilot"
    assert COPILOT_DESCRIPTOR.cli_command == "copilot"


def test_preflight_raises_when_binary_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If copilot is not on PATH, preflight must raise."""
    monkeypatch.setattr(shutil, "which", lambda _bin: None)
    backend = CopilotBackend()
    with pytest.raises(RuntimeError, match="copilot"):
        backend.preflight(SessionSpec(cwd="/tmp"))


def test_capabilities_stable_across_calls() -> None:
    """Repeated capabilities() invocations must return equal instances."""
    backend = CopilotBackend()
    assert backend.capabilities() == backend.capabilities()