"""Cursor backend preflight + capability smoke tests."""

from __future__ import annotations

import shutil
from dataclasses import fields

import pytest

from orchestratord.spi.backend import SessionSpec
from orchestratord_cursor.backend import CursorBackend
from orchestratord_cursor.descriptor import CURSOR_DESCRIPTOR


def test_descriptor_capabilities_match_backend() -> None:
    """Descriptor bit set must equal backend.capabilities() output.

    Pins ``tests/test_capability_drift.py`` drift-detector locally so a
    backend change without descriptor update is caught at unit-test time.
    """
    backend = CursorBackend()
    descriptor_bits = set(CURSOR_DESCRIPTOR.capabilities)
    backend_bits = {
        f.name
        for f in fields(backend.capabilities())
        if getattr(backend.capabilities(), f.name)
    }
    assert backend_bits == descriptor_bits, (
        f"cursor: backend bits {backend_bits} drifted from descriptor "
        f"{descriptor_bits}"
    )


def test_descriptor_metadata() -> None:
    """Descriptor identifies the package and CLI surface correctly."""
    assert CURSOR_DESCRIPTOR.name == "cursor"
    assert CURSOR_DESCRIPTOR.family.value == "Cli"
    assert CURSOR_DESCRIPTOR.backend_package == "orchestratord-cursor"
    assert CURSOR_DESCRIPTOR.cli_command == "cursor-agent"


def test_preflight_raises_when_binary_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If cursor-agent is not on PATH, preflight must raise."""
    monkeypatch.setattr(shutil, "which", lambda _bin: None)
    backend = CursorBackend()
    with pytest.raises(RuntimeError, match="cursor-agent"):
        backend.preflight(SessionSpec(cwd="/tmp"))


def test_capabilities_stable_across_calls() -> None:
    """Repeated capabilities() invocations must return equal instances."""
    backend = CursorBackend()
    assert backend.capabilities() == backend.capabilities()