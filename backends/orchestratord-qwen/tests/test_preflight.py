"""Qwen backend preflight + capability smoke tests.

Pins the only P1 backend in §8 that genuinely claims
``streaming_deltas=True`` — a regression here means the qwen backend
silently dropped real streaming (the orchestrator's split-whole-text
degradation would mask it).
"""

from __future__ import annotations

import shutil
from dataclasses import fields

import pytest

from orchestratord.spi.backend import SessionSpec
from orchestratord_qwen.backend import QwenBackend
from orchestratord_qwen.descriptor import QWEN_DESCRIPTOR


def test_descriptor_capabilities_match_backend() -> None:
    """Descriptor bit set must equal backend.capabilities() output."""
    backend = QwenBackend()
    descriptor_bits = set(QWEN_DESCRIPTOR.capabilities)
    backend_bits = {
        f.name
        for f in fields(backend.capabilities())
        if getattr(backend.capabilities(), f.name)
    }
    assert backend_bits == descriptor_bits, (
        f"qwen: backend bits {backend_bits} drifted from descriptor "
        f"{descriptor_bits}"
    )


def test_descriptor_metadata() -> None:
    """Descriptor identifies the package and CLI surface correctly."""
    assert QWEN_DESCRIPTOR.name == "qwen"
    assert QWEN_DESCRIPTOR.family.value == "Cli"
    assert QWEN_DESCRIPTOR.backend_package == "orchestratord-qwen"
    assert QWEN_DESCRIPTOR.cli_command == "qwen"
    # stream-json mode must be advertised via cli_args_probe so the
    # install-time runtime detection triggers the right invocation.
    assert "stream-json" in QWEN_DESCRIPTOR.cli_args_probe


def test_streaming_deltas_bit_declared() -> None:
    """qwen is the only P1 backend with streaming_deltas=True (§8.1)."""
    backend = QwenBackend()
    assert backend.capabilities().streaming_deltas is True


def test_preflight_raises_when_binary_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If qwen is not on PATH, preflight must raise."""
    monkeypatch.setattr(shutil, "which", lambda _bin: None)
    backend = QwenBackend()
    with pytest.raises(RuntimeError, match="qwen"):
        backend.preflight(SessionSpec(cwd="/tmp"))


def test_capabilities_stable_across_calls() -> None:
    """Repeated capabilities() invocations must return equal instances."""
    backend = QwenBackend()
    assert backend.capabilities() == backend.capabilities()