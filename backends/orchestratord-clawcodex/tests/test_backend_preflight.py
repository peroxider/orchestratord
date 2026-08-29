"""Clawcodex-specific preflight tests.

Lives in the backend package because it stubs ``extensions.api.query``,
the namespace the production clawcodex SDK uses.  Tests for other
backends (codex / hermes / opencode / dsh) and the SPI-level
``DegradingBackend`` wrapper remain at the repo-root ``tests/`` tree.

The preflight contract: validate readiness without starting a backend
process or contacting a provider.
"""

from __future__ import annotations

import builtins
import sys
import types
from unittest.mock import Mock

import pytest

from orchestratord.spi.backend import SessionSpec
from orchestratord_clawcodex.backend import ClawcodexBackend


@pytest.fixture
def spec() -> SessionSpec:
    """A provider-neutral session specification for preflight checks."""
    return SessionSpec(cwd="/workspace", provider="mock-provider")


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
