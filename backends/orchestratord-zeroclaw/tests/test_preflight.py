"""Zeroclaw backend preflight + capability smoke tests."""

from __future__ import annotations

import shutil
from dataclasses import fields

import pytest

from orchestratord.spi.backend import SessionSpec
from orchestratord_zeroclaw.backend import ZeroclawBackend
from orchestratord_zeroclaw.descriptor import ZEROCLAW_DESCRIPTOR


def test_descriptor_capabilities_match_backend() -> None:
    """Descriptor bit set must equal backend.capabilities() output."""
    backend = ZeroclawBackend()
    descriptor_bits = set(ZEROCLAW_DESCRIPTOR.capabilities)
    backend_bits = {
        f.name
        for f in fields(backend.capabilities())
        if getattr(backend.capabilities(), f.name)
    }
    assert backend_bits == descriptor_bits, (
        f"zeroclaw: backend bits {backend_bits} drifted from descriptor "
        f"{descriptor_bits}"
    )


def test_descriptor_metadata() -> None:
    """Descriptor identifies the package and CLI surface correctly."""
    assert ZEROCLAW_DESCRIPTOR.name == "zeroclaw"
    assert ZEROCLAW_DESCRIPTOR.family.value == "Cli"
    assert ZEROCLAW_DESCRIPTOR.backend_package == "orchestratord-zeroclaw"
    assert ZEROCLAW_DESCRIPTOR.cli_command == "zeroclaw"


def test_preflight_raises_when_binary_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If zeroclaw is not on PATH, preflight must raise."""
    monkeypatch.setattr(shutil, "which", lambda _bin: None)
    backend = ZeroclawBackend()
    with pytest.raises(RuntimeError, match="zeroclaw"):
        backend.preflight(SessionSpec(cwd="/tmp"))


def test_capabilities_stable_across_calls() -> None:
    """Repeated capabilities() invocations must return equal instances."""
    backend = ZeroclawBackend()
    assert backend.capabilities() == backend.capabilities()
