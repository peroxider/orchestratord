"""Tests for :func:`orchestratord.backend_registry.resolve_backend`.

DESIGN_two_tier_backend_registry.md §3.5 验收：

* ``resolve_backend("clawcodex-dev")`` 返回 :class:`AgentBackend`（DegradingBackend 包装）
* ``resolve_backend("nonexistent")`` 抛 :class:`BackendNotFoundError`
* descriptor 与实现 capability 不一致时（``strict=True``）抛 :class:`BackendMismatchError`
"""

from __future__ import annotations

import importlib.metadata as md
from dataclasses import fields

import pytest

from orchestratord.backend_registry import (
    BackendMismatchError,
    BackendNotFoundError,
    discover_descriptors,
    list_backends,
    resolve_backend,
)
from orchestratord.spi.backend_descriptor import BackendDescriptor, BackendFamily
from orchestratord.spi.degradation import DegradingBackend


def _known_descriptor_names() -> list[str]:
    """Sorted list of currently registered descriptor keys (skips test if empty)."""
    names = sorted(discover_descriptors())
    if not names:
        pytest.skip(
            "no orchestratord.backend_descriptors entry-points installed",
            allow_module_level=True,
        )
    return names


def test_resolve_backend_returns_degrading_backend_instance() -> None:
    """``resolve_backend("clawcodex-dev")`` returns a ``DegradingBackend``.

    The registry always wraps implementations in :class:`DegradingBackend`
    to enforce core degradation paths uniformly.
    """
    descs = discover_descriptors()
    if "clawcodex-dev" not in descs:
        pytest.skip("clawcodex descriptor not installed")
    backend = resolve_backend("clawcodex-dev")
    assert isinstance(backend, DegradingBackend)


def test_resolve_backend_unknown_identifier_raises() -> None:
    """``resolve_backend("nonexistent-xyz")`` raises :class:`BackendNotFoundError`."""
    with pytest.raises(BackendNotFoundError) as excinfo:
        resolve_backend("nonexistent-xyz")
    assert "nonexistent-xyz" in str(excinfo.value)


@pytest.mark.parametrize("identifier", _known_descriptor_names())
def test_resolve_backend_smoke(identifier: str) -> None:
    """Every registered descriptor must resolve to a backend with sane ``name``."""
    backend = resolve_backend(identifier)
    assert hasattr(backend, "name")
    assert isinstance(backend.name, str) and backend.name


@pytest.mark.parametrize("identifier", _known_descriptor_names())
def test_resolve_backend_capabilities_callable(identifier: str) -> None:
    """The resolved backend's ``capabilities()`` returns a dataclass with bits
    that overlap the descriptor declaration (sanity, not strict equality).
    """
    desc = discover_descriptors()[identifier]
    backend = resolve_backend(identifier)
    caps = backend.capabilities()
    assert hasattr(caps, "__dataclass_fields__")
    actual = {f.name for f in fields(caps) if getattr(caps, f.name)}
    declared = set(desc.capabilities)
    assert actual & declared, (
        f"{identifier}: no overlap between descriptor bits "
        f"{sorted(declared)} and actual bits {sorted(actual)}"
    )


def test_strict_mode_succeeds_for_consistent_backend() -> None:
    """``strict=True`` passes when descriptor matches backend bits exactly."""
    descs = discover_descriptors()
    if "hermes" not in descs:
        pytest.skip("hermes backend not installed")
    backend = resolve_backend("hermes", strict=True)
    assert isinstance(backend, DegradingBackend)


def test_strict_mode_detects_intentional_drift(monkeypatch) -> None:
    """``strict=True`` raises :class:`BackendMismatchError` when descriptor
    bits disagree with the backend's :py:meth:`capabilities`.

    We patch the in-memory descriptor (loaded earlier by entry_points) so we
    don't touch on-disk source — a clean way to simulate drift.
    """
    descs = discover_descriptors()
    if "clawcodex-dev" not in descs:
        pytest.skip("clawcodex descriptor not installed")
    real_desc = descs["clawcodex-dev"]
    sample_backend = resolve_backend("clawcodex-dev")
    actual_bits = {
        f.name
        for f in fields(sample_backend.capabilities())
        if getattr(sample_backend.capabilities(), f.name)
    }
    declared_bits = set(real_desc.capabilities)
    drop = sorted(actual_bits & declared_bits)
    if not drop:
        pytest.skip("no overlapping bit to drop — cannot simulate drift")
    tampered_caps = frozenset(declared_bits - {drop[0]})
    tampered = BackendDescriptor(
        name=real_desc.name,
        display_name=real_desc.display_name,
        family=real_desc.family,
        backend_package=real_desc.backend_package,
        capabilities=tampered_caps,
        cli_command=real_desc.cli_command,
        cli_args_probe=real_desc.cli_args_probe,
        env_prefix=real_desc.env_prefix,
        launch_header=real_desc.launch_header,
        model_discovery=real_desc.model_discovery,
        extra_metadata=real_desc.extra_metadata,
    )

    def fake_discover():
        return {real_desc.name: tampered}

    monkeypatch.setattr(
        "orchestratord.backend_registry.discover_descriptors",
        fake_discover,
    )
    with pytest.raises(BackendMismatchError) as excinfo:
        resolve_backend("clawcodex-dev", strict=True)
    assert "drift" in str(excinfo.value).lower()


def test_list_backends_matches_discover_descriptors() -> None:
    """``list_backends()`` is a thin view over ``discover_descriptors()``."""
    descs = discover_descriptors()
    listed = list_backends()
    listed_names = {entry["name"] for entry in listed}
    assert listed_names == set(descs)
    for entry in listed:
        d = descs[entry["name"]]
        assert entry["family"] == d.family.value
        assert entry["display_name"] == d.display_name
        assert entry["backend_package"] == d.backend_package


def test_codex_dual_descriptors_resolve_to_same_implementation() -> None:
    """``codex-cli`` and ``codex-app-server`` share a :class:`AgentBackend`.

    Both descriptors target ``backend_package="orchestratord-codex"`` →
    :func:`_resolve_implementation_class` returns the same class. The runtime
    inside picks cli vs app-server via the ``_detect_runtime()`` probe; the
    descriptor simply declares which bit set is expected for that runtime.
    """
    descs = discover_descriptors()
    if "codex-cli" not in descs or "codex-app-server" not in descs:
        pytest.skip("codex descriptors not installed")
    cli_b = resolve_backend("codex-cli")
    as_b = resolve_backend("codex-app-server")
    assert type(cli_b) is type(as_b), (
        "codex-cli and codex-app-server must share an implementation class"
    )


def test_codex_descriptor_forces_preferred_runtime() -> None:
    """Resolving a codex descriptor forwards ``extra_metadata["prefer"]``.

    ``resolve_backend("codex-app-server")`` must construct the backend with
    ``prefer="as"`` (streaming deltas lit) and ``resolve_backend("codex-cli")``
    with ``prefer="cli"`` (Cli-only bits) — bypassing the runtime probe.
    Per ``DESIGN_backends_hardening.md`` §1.2 / §3.2.
    """
    descs = discover_descriptors()
    if "codex-cli" not in descs or "codex-app-server" not in descs:
        pytest.skip("codex descriptors not installed")

    as_backend = resolve_backend("codex-app-server")
    caps_as = as_backend.capabilities()
    assert caps_as.streaming_deltas is True
    assert caps_as.interrupt is True
    assert caps_as.approval_hooks is True

    cli_backend = resolve_backend("codex-cli")
    caps_cli = cli_backend.capabilities()
    assert caps_cli.streaming_deltas is False
    assert caps_cli.resumable is True


def test_daemon_backend_resolution_accepts_descriptor_name(monkeypatch) -> None:
    """The daemon CLI must accept names advertised by ``backend list``."""
    from orchestratord.cli.server import _resolve_daemon_backend

    expected = object()
    monkeypatch.setattr(
        "orchestratord.backend_registry.resolve_backend",
        lambda identifier, strict: expected if identifier == "codex-cli" else None,
    )

    assert _resolve_daemon_backend("codex-cli") is expected


def test_daemon_backend_resolution_keeps_legacy_implementation_name(monkeypatch) -> None:
    """Existing ``--backend codex`` scripts remain supported."""
    from orchestratord.backend_registry import BackendNotFoundError
    from orchestratord.cli.server import _resolve_daemon_backend

    expected = object()

    def missing_descriptor(_identifier, *, strict):
        raise BackendNotFoundError("not a descriptor")

    monkeypatch.setattr(
        "orchestratord.backend_registry.resolve_backend",
        missing_descriptor,
    )
    monkeypatch.setattr(
        "orchestratord.backend_registry.discover_backends",
        lambda: {"codex": expected},
    )

    assert _resolve_daemon_backend("codex") is expected


def test_descriptor_entry_point_group_constant() -> None:
    """``DESCRIPTOR_ENTRY_POINT_GROUP`` matches the string used by all backend
    ``pyproject.toml`` files (drift detector relies on this)."""
    from orchestratord.backend_registry import DESCRIPTOR_ENTRY_POINT_GROUP

    assert DESCRIPTOR_ENTRY_POINT_GROUP == "orchestratord.backend_descriptors"
    eps = list(md.entry_points(group=DESCRIPTOR_ENTRY_POINT_GROUP))
    assert eps, "no entry-points under orchestratord.backend_descriptors"


def test_impl_entry_point_group_constant() -> None:
    from orchestratord.backend_registry import IMPL_ENTRY_POINT_GROUP

    assert IMPL_ENTRY_POINT_GROUP == "orchestratord.backends"
    eps = list(md.entry_points(group=IMPL_ENTRY_POINT_GROUP))
    assert eps, "no entry-points under orchestratord.backends"


def test_backend_family_values_are_strings() -> None:
    """Enum values are the string constants the registry / CLI historically emit."""
    expected = {"InProcess", "SdkProcess", "Protocol", "Cli"}
    actual = {f.value for f in BackendFamily}
    assert actual == expected
