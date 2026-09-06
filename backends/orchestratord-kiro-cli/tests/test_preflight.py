"""Kiro backend preflight + capability smoke tests."""

from __future__ import annotations

import shutil
from dataclasses import fields

import pytest

from orchestratord.spi.backend import SessionSpec
from orchestratord_kiro_cli.backend import KiroBackend
from orchestratord_kiro_cli.descriptor import KIRO_DESCRIPTOR


def test_descriptor_capabilities_match_backend() -> None:
    """Descriptor bit set must equal backend.capabilities() output."""
    backend = KiroBackend()
    descriptor_bits = set(KIRO_DESCRIPTOR.capabilities)
    backend_bits = {
        f.name
        for f in fields(backend.capabilities())
        if getattr(backend.capabilities(), f.name)
    }
    assert backend_bits == descriptor_bits, (
        f"kiro-cli: backend bits {backend_bits} drifted from descriptor "
        f"{descriptor_bits}"
    )


def test_descriptor_metadata() -> None:
    """Descriptor identifies the package and CLI surface correctly."""
    assert KIRO_DESCRIPTOR.name == "kiro-cli"
    assert KIRO_DESCRIPTOR.family.value == "Cli"
    assert KIRO_DESCRIPTOR.backend_package == "orchestratord-kiro-cli"
    assert KIRO_DESCRIPTOR.cli_command == "kiro"


def test_preflight_raises_when_binary_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If kiro is not on PATH, preflight must raise."""
    monkeypatch.setattr(shutil, "which", lambda _bin: None)
    backend = KiroBackend()
    with pytest.raises(RuntimeError, match="kiro"):
        backend.preflight(SessionSpec(cwd="/tmp"))


def test_capabilities_stable_across_calls() -> None:
    """Repeated capabilities() invocations must return equal instances."""
    backend = KiroBackend()
    assert backend.capabilities() == backend.capabilities()
