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
from types import SimpleNamespace
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


def test_dsh_preflight_rejects_non_deepseek_provider(spec: SessionSpec) -> None:
    """A provider without a runtime adapter must fail preflight
    with an actionable message — not die mid-stage with the opaque
    "no adapter registered for provider X" runtime error.
    """
    spec.provider = "anthropic"

    with pytest.raises(RuntimeError) as raised:
        DshBackend().preflight(spec)

    message = str(raised.value)
    assert "anthropic" in message
    assert "deepseek-official" in message
    assert "agent.provider" in message


def test_dsh_preflight_accepts_deepseek_provider(
    spec: SessionSpec, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec.provider = "deepseek-official"
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    DshBackend().preflight(spec)  # must not raise


def test_dsh_preflight_allows_missing_provider_default(
    spec: SessionSpec, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No provider configured → the backend default applies; preflight
    must not reject a spec that would have worked.
    """
    spec.provider = None
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    DshBackend().preflight(spec)  # must not raise


def test_dsh_preflight_requires_credential(
    spec: SessionSpec, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no api_key configured and no DEEPSEEK_API_KEY in the
    environment, preflight must fail with an actionable message — the
    historical behaviour reported ready and deferred the failure to
    mid-run as an opaque error.
    """
    spec.provider = "deepseek-official"
    spec.api_key = None
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)

    with pytest.raises(RuntimeError) as raised:
        DshBackend().preflight(spec)

    message = str(raised.value)
    assert "DEEPSEEK_API_KEY" in message


def test_dsh_preflight_accepts_env_credential(
    spec: SessionSpec, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec.provider = "deepseek-official"
    spec.api_key = None
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    DshBackend().preflight(spec)  # must not raise


def test_dsh_preflight_accepts_explicit_api_key(
    spec: SessionSpec, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec.provider = "deepseek-official"
    spec.api_key = "sk-configured"
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    DshBackend().preflight(spec)  # must not raise


def test_dsh_preflight_rejects_unresolvable_api_key_ref(
    spec: SessionSpec, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A '$VAR' style api_key reference whose variable is missing must
    be reported at preflight, not mid-run.
    """
    spec.provider = "deepseek-official"
    spec.api_key = "${DSH_MISSING_KEY}"
    monkeypatch.delenv("DSH_MISSING_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)

    with pytest.raises(RuntimeError) as raised:
        DshBackend().preflight(spec)

    assert "DSH_MISSING_KEY" in str(raised.value)


def test_doctor_reports_unready_without_credential(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """`backend doctor dsh` must report unready when
    DEEPSEEK_API_KEY is absent (historically reported ready).
    """
    import sys

    from orchestratord.cli import backend as backend_cli

    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    args = SimpleNamespace(name="dsh", backend_subcommand="doctor")

    rc = backend_cli.run(args)

    assert rc == 1
    err = capsys.readouterr().err
    assert "NOT ready" in err
    assert "DEEPSEEK_API_KEY" in err


def test_doctor_reports_ready_with_credential(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    args = SimpleNamespace(name="dsh", backend_subcommand="doctor")

    from orchestratord.cli import backend as backend_cli

    rc = backend_cli.run(args)

    assert rc == 0
    assert "ready" in capsys.readouterr().out
