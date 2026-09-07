"""Reasonix backend preflight + capability smoke tests."""

from __future__ import annotations

import shutil
from dataclasses import fields

import pytest

from orchestratord.spi.backend import SessionSpec
from orchestratord_reasonix.backend import ReasonixBackend
from orchestratord_reasonix.descriptor import REASONIX_DESCRIPTOR


def test_descriptor_capabilities_match_backend() -> None:
    """Descriptor bit set must equal backend.capabilities() output."""
    backend = ReasonixBackend()
    descriptor_bits = set(REASONIX_DESCRIPTOR.capabilities)
    backend_bits = {
        f.name
        for f in fields(backend.capabilities())
        if getattr(backend.capabilities(), f.name)
    }
    assert backend_bits == descriptor_bits, (
        f"reasonix: backend bits {backend_bits} drifted from descriptor "
        f"{descriptor_bits}"
    )


def test_descriptor_metadata() -> None:
    """Descriptor identifies the package and CLI surface correctly."""
    assert REASONIX_DESCRIPTOR.name == "reasonix"
    assert REASONIX_DESCRIPTOR.family.value == "Cli"
    assert REASONIX_DESCRIPTOR.backend_package == "orchestratord-reasonix"
    assert REASONIX_DESCRIPTOR.cli_command == "reasonix"


def test_preflight_raises_when_binary_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If reasonix is not on PATH, preflight must raise."""
    monkeypatch.setattr(shutil, "which", lambda _bin: None)
    backend = ReasonixBackend()
    with pytest.raises(RuntimeError, match="reasonix"):
        backend.preflight(SessionSpec(cwd="/tmp"))


def test_capabilities_stable_across_calls() -> None:
    """Repeated capabilities() invocations must return equal instances."""
    backend = ReasonixBackend()
    assert backend.capabilities() == backend.capabilities()
