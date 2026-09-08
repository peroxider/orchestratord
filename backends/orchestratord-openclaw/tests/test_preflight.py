"""Openclaw backend preflight + capability smoke tests."""

from __future__ import annotations

import shutil
from dataclasses import fields

import pytest

from orchestratord.spi.backend import SessionSpec
from orchestratord_openclaw.backend import OpenclawBackend
from orchestratord_openclaw.descriptor import OPENCLAW_DESCRIPTOR


def test_descriptor_capabilities_match_backend() -> None:
    """Descriptor bit set must equal backend.capabilities() output."""
    backend = OpenclawBackend()
    descriptor_bits = set(OPENCLAW_DESCRIPTOR.capabilities)
    backend_bits = {
        f.name
        for f in fields(backend.capabilities())
        if getattr(backend.capabilities(), f.name)
    }
    assert backend_bits == descriptor_bits, (
        f"openclaw: backend bits {backend_bits} drifted from descriptor "
        f"{descriptor_bits}"
    )


def test_descriptor_metadata() -> None:
    """Descriptor identifies the package and CLI surface correctly."""
    assert OPENCLAW_DESCRIPTOR.name == "openclaw"
    assert OPENCLAW_DESCRIPTOR.family.value == "Cli"
    assert OPENCLAW_DESCRIPTOR.backend_package == "orchestratord-openclaw"
    assert OPENCLAW_DESCRIPTOR.cli_command == "openclaw"


def test_preflight_raises_when_binary_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If openclaw is not on PATH, preflight must raise."""
    monkeypatch.setattr(shutil, "which", lambda _bin: None)
    backend = OpenclawBackend()
    with pytest.raises(RuntimeError, match="openclaw"):
        backend.preflight(SessionSpec(cwd="/tmp"))


def test_capabilities_stable_across_calls() -> None:
    """Repeated capabilities() invocations must return equal instances."""
    backend = OpenclawBackend()
    assert backend.capabilities() == backend.capabilities()
