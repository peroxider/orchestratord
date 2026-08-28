"""Contract tests for backend startup pre-flight validation.

These tests deliberately replace local binaries, optional imports, and provider
configuration.  A preflight check must therefore remain side-effect free: it
may validate readiness, but it must not start a backend process or contact a
provider.
"""

from __future__ import annotations

import builtins
import sys
import types
from unittest.mock import Mock

import pytest

from orchestratord.spi.backend import SessionSpec
from orchestratord.spi.capabilities import BackendCapabilities
from orchestratord.spi.degradation import DegradingBackend
from orchestratord_clawcodex.backend import ClawcodexBackend
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


def _install_clawcodex_imports(
    monkeypatch: pytest.MonkeyPatch,
    get_provider_config: Mock,
) -> None:
    """Provide the two Clawcodex runtime imports without loading its SDK."""
    extensions = types.ModuleType("extensions")
    extensions.__path__ = []  # type: ignore[attr-defined]
    api = types.ModuleType("extensions.api")
    api.__path__ = []  # type: ignore[attr-defined]
    query = types.ModuleType("extensions.api.query")
    query.QueryConfig = object
    query.QueryRunner = object
    api.query = query  # type: ignore[attr-defined]
    extensions.api = api  # type: ignore[attr-defined]

    source = types.ModuleType("src")
    source.__path__ = []  # type: ignore[attr-defined]
    config = types.ModuleType("src.config")
    config.get_provider_config = get_provider_config
    source.config = config  # type: ignore[attr-defined]

    monkeypatch.setitem(sys.modules, "extensions", extensions)
    monkeypatch.setitem(sys.modules, "extensions.api", api)
    monkeypatch.setitem(sys.modules, "extensions.api.query", query)
    monkeypatch.setitem(sys.modules, "src", source)
    monkeypatch.setitem(sys.modules, "src.config", config)


def test_clawcodex_preflight_accepts_mocked_source_api_and_provider_config(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
    spec: SessionSpec,
) -> None:
    """Clawcodex validates its API import and delegates credential lookup."""
    provider_config = Mock(return_value={"api_key": "test-only-key"})
    _install_clawcodex_imports(monkeypatch, provider_config)
    monkeypatch.setenv("CLAWCODEX_SOURCE", str(tmp_path))

    ClawcodexBackend().preflight(spec)

    provider_config.assert_called_once_with("mock-provider")


def test_clawcodex_preflight_rejects_missing_source_directory(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
    spec: SessionSpec,
) -> None:
    """A stale CLAWCODEX_SOURCE fails before importing the runtime."""
    missing_source = tmp_path / "missing-clawcodex-source"
    monkeypatch.setenv("CLAWCODEX_SOURCE", str(missing_source))

    with pytest.raises(RuntimeError) as raised:
        ClawcodexBackend().preflight(spec)

    message = str(raised.value).lower()
    assert "clawcodex_source" in message
    assert "directory" in message


def test_clawcodex_preflight_rejects_missing_extensions_api(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
    spec: SessionSpec,
) -> None:
    """A source tree without extensions.api.query has an actionable error."""
    monkeypatch.setenv("CLAWCODEX_SOURCE", str(tmp_path))
    for name in ("extensions.api.query", "extensions.api", "extensions"):
        monkeypatch.delitem(sys.modules, name, raising=False)

    original_import = builtins.__import__

    def _reject_extensions(name: str, *args, **kwargs):
        if name == "extensions" or name.startswith("extensions."):
            raise ModuleNotFoundError(f"No module named {name!r}")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _reject_extensions)

    with pytest.raises(RuntimeError) as raised:
        ClawcodexBackend().preflight(spec)

    message = str(raised.value).lower()
    assert "extensions.api.query" in message
    assert "clawcodex" in message


def test_clawcodex_preflight_rejects_provider_without_api_key(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
    spec: SessionSpec,
) -> None:
    """Provider readiness remains Clawcodex's configuration responsibility."""
    provider_config = Mock(return_value={})
    _install_clawcodex_imports(monkeypatch, provider_config)
    monkeypatch.setenv("CLAWCODEX_SOURCE", str(tmp_path))

    with pytest.raises(RuntimeError) as raised:
        ClawcodexBackend().preflight(spec)

    provider_config.assert_called_once_with("mock-provider")
    message = str(raised.value).lower()
    assert "api key" in message
    assert "mock-provider" in message


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
    original_import = builtins.__import__

    def _reject_httpx(name: str, *args, **kwargs):
        if name == "httpx" or name.startswith("httpx."):
            raise ModuleNotFoundError(f"No module named {name!r}")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _reject_httpx)

    with pytest.raises(RuntimeError) as raised:
        OpenCodeBackend().preflight(spec)

    message = str(raised.value).lower()
    assert "httpx" in message


def test_dsh_preflight_rejects_incomplete_harness_sdk(
    monkeypatch: pytest.MonkeyPatch,
    spec: SessionSpec,
) -> None:
    """DSH names the required API types when its SDK is incompatible."""
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
