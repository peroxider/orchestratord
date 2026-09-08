"""Kimi backend preflight + capability smoke tests."""

from __future__ import annotations

import shutil
from dataclasses import fields

import pytest

from orchestratord.spi.backend import SessionSpec
from orchestratord_kimi.backend import KimiBackend
from orchestratord_kimi.descriptor import KIMI_DESCRIPTOR


def test_descriptor_capabilities_match_backend() -> None:
    """Descriptor bit set must equal backend.capabilities() output."""
    backend = KimiBackend()
    descriptor_bits = set(KIMI_DESCRIPTOR.capabilities)
    backend_bits = {
        f.name
        for f in fields(backend.capabilities())
        if getattr(backend.capabilities(), f.name)
    }
    assert backend_bits == descriptor_bits, (
        f"kimi: backend bits {backend_bits} drifted from descriptor "
        f"{descriptor_bits}"
    )


def test_descriptor_metadata() -> None:
    """Descriptor identifies the package and CLI surface correctly."""
    assert KIMI_DESCRIPTOR.name == "kimi"
    assert KIMI_DESCRIPTOR.family.value == "Cli"
    assert KIMI_DESCRIPTOR.backend_package == "orchestratord-kimi"
    assert KIMI_DESCRIPTOR.cli_command == "kimi"


def test_preflight_raises_when_binary_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If kimi is not on PATH, preflight must raise."""
    monkeypatch.setattr(shutil, "which", lambda _bin: None)
    backend = KimiBackend()
    with pytest.raises(RuntimeError, match="kimi"):
        backend.preflight(SessionSpec(cwd="/tmp"))


def test_capabilities_stable_across_calls() -> None:
    """Repeated capabilities() invocations must return equal instances."""
    backend = KimiBackend()
    assert backend.capabilities() == backend.capabilities()