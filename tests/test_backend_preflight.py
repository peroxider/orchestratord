"""Contract tests for backend startup pre-flight validation.

Clawcodex-specific tests live in
``backends/orchestratord-clawcodex/tests/test_backend_preflight.py``
because they stub the clawcodex SDK namespace that the production
backend code imports.  This file retains the SPI-level wrapper
test and the per-backend (codex / hermes / opencode / dsh) preflight
tests.

The preflight contract: validate readiness without starting a backend
process or contacting a provider.
"""

from __future__ import annotations

import sys
from unittest.mock import Mock

import pytest

from orchestratord.spi.backend import SessionSpec
from orchestratord.spi.capabilities import BackendCapabilities
from orchestratord.spi.degradation import DegradingBackend
from orchestratord_codex.backend import CodexBackend
from orchestratord_dsh.backend import DshBackend
from orchestratord_hermes.backend import HermesBackend
from orchestratord_opencode.backend import OpenCodeBackend


@pytest.fixture
def spec() -> SessionSpec:
    """A provider-neutral session specification for preflight checks."""
    return SessionSpec(cwd="/workspace", provider="mock-provider")


class _BackendWithPreflight:
    name = "inner"
    display_name = "Inner"

    def __init__(self) -> None:
        self.preflight = Mock()

    def capabilities(self) -> BackendCapabilities:
        return BackendCapabilities()

    def create_session(self, spec: SessionSpec):
        raise AssertionError("preflight tests must not create a session")

    def dispose(self) -> None:
        pass


class _BackendWithoutPreflight:
    name = "inner"
    display_name = "Inner"

    def capabilities(self) -> BackendCapabilities:
        return BackendCapabilities()

    def create_session(self, spec: SessionSpec):
        raise AssertionError("preflight tests must not create a session")

    def dispose(self) -> None:
        pass


def test_degrading_backend_forwards_preflight_to_inner_backend(spec: SessionSpec) -> None:
    """The wrapper preserves an inner backend's readiness validation."""
    inner = _BackendWithPreflight()

    DegradingBackend(inner).preflight(spec)

    inner.preflight.assert_called_once_with(spec)


def test_degrading_backend_preflight_is_noop_when_inner_lacks_it(
    spec: SessionSpec,
) -> None:
    """Legacy backends without preflight support remain compatible."""
    DegradingBackend(_BackendWithoutPreflight()).preflight(spec)


def test_codex_preflight_rejects_missing_executable(
    monkeypatch: pytest.MonkeyPatch,
    spec: SessionSpec,
) -> None:
    """Codex reports a missing CLI instead of deferring failure to a session."""
    monkeypatch.setenv("PATH", "")
    backend = CodexBackend()

    with pytest.raises(RuntimeError) as raised:
        backend.preflight(spec)

    message = str(raised.value).lower()
    assert "codex" in message
    assert "path" in message


def test_hermes_preflight_rejects_missing_executable(
    monkeypatch: pytest.MonkeyPatch,
    spec: SessionSpec,
) -> None:
    """Hermes reports a missing CLI instead of deferring failure to a session."""
    monkeypatch.setenv("PATH", "")

    with pytest.raises(RuntimeError) as raised:
        HermesBackend().preflight(spec)

    message = str(raised.value).lower()
    assert "hermes" in message
    assert "path" in message


def test_opencode_preflight_rejects_missing_executable(
    monkeypatch: pytest.MonkeyPatch,
    spec: SessionSpec,
) -> None:
    """OpenCode reports its missing serve executable before session startup."""
    monkeypatch.setenv("PATH", "")

    with pytest.raises(RuntimeError) as raised:
        OpenCodeBackend().preflight(spec)

    message = str(raised.value).lower()
    assert "opencode" in message
    assert "path" in message


def test_opencode_preflight_rejects_missing_httpx(
    monkeypatch: pytest.MonkeyPatch,
    spec: SessionSpec,
) -> None:
    """The optional HTTP client failure is presented as a backend error."""
    monkeypatch.delitem(sys.modules, "httpx", raising=False)
    import builtins as _builtins

    original_import = _builtins.__import__

    def _reject_httpx(name: str, *args, **kwargs):
        if name == "httpx" or name.startswith("httpx."):
            raise ModuleNotFoundError(f"No module named {name!r}")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(_builtins, "__import__", _reject_httpx)

    with pytest.raises(RuntimeError) as raised:
        OpenCodeBackend().preflight(spec)

    message = str(raised.value).lower()
    assert "httpx" in message


def test_dsh_preflight_rejects_incomplete_harness_sdk(
    monkeypatch: pytest.MonkeyPatch,
    spec: SessionSpec,
) -> None:
    """DSH names the required API types when its SDK is incompatible."""
    import types

    package = types.ModuleType("deepseek_harness")
    package.__path__ = []  # type: ignore[attr-defined]
    api = types.ModuleType("deepseek_harness.api")
    package.api = api  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "deepseek_harness", package)
    monkeypatch.setitem(sys.modules, "deepseek_harness.api", api)

    with pytest.raises(RuntimeError) as raised:
        DshBackend().preflight(spec)

    message = str(raised.value)
    assert "DeepSeekHarness" in message
    assert "DeepSeekHarnessConfig" in message
