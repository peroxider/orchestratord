"""§8.2.2 builtin-runtime derivation tests (``omp`` → ``pi``).

DB-free unit tests for :func:`resolve_backend`'s derivation fallback: a
descriptor whose own ``backend_package`` has no registered implementation
falls back to the implementation of the runtime its ``runtime_id`` (then
``protocol_family``) points at. Entry-point loading is monkeypatched, so
no real backend packages need to be installed.

The fake implementation classes live in this module, whose ``__module__``
root is ``tests`` — the source-runtime descriptor therefore declares
``backend_package="tests"`` to match ``cls.__module__.split(".")[0]``.

Reference: docs/FEATURE_GAP_VS_MULTICA.md §8.2.2 / §8.5.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, ClassVar

import pytest

from orchestratord.backend_registry import (
    BackendMismatchError,
    BackendNotFoundError,
    _derive_implementation_descriptor,
    list_backends,
    resolve_backend,
)
from orchestratord.spi.backend_descriptor import (
    BackendDescriptor,
    BackendFamily,
)
from orchestratord.spi.capabilities import BackendCapabilities


@dataclass(frozen=True)
class _FakeEp:
    """Entry-point double: ``.load()`` returns the captured object."""

    value: Any

    def load(self) -> Any:
        return self.value


class _PiImpl:
    """Minimal AgentBackend double for the ``pi`` source runtime."""

    created: ClassVar[list["_PiImpl"]] = []

    def __init__(self, prefer: str | None = None) -> None:
        self.prefer = prefer
        self.name = "pi"
        self.display_name = "Pi"
        type(self).created.append(self)

    def capabilities(self) -> BackendCapabilities:
        return BackendCapabilities()

    def preflight(self, spec: Any) -> None:
        return None

    def create_session(self, spec: Any):
        raise NotImplementedError

    def get_task_registry(self):
        return None

    def dispose(self) -> None:
        return None


class _OmpImpl(_PiImpl):
    """Implementation double registered under the ``omp`` runtime itself."""

    created: ClassVar[list["_OmpImpl"]] = []

    def __init__(self, prefer: str | None = None) -> None:
        super().__init__(prefer)
        self.name = "omp"
        self.display_name = "Oh My Pi"


def _module_root(cls: type) -> str:
    """Mirror the registry's package matcher on a fake impl class.

    pytest imports this module without a ``tests`` package prefix (no
    ``__init__.py``), so deriving the root from the class keeps the fake
    descriptor's ``backend_package`` correct regardless of import style.
    """
    return cls.__module__.split(".")[0]


def _descriptor(
    name: str,
    *,
    package: str,
    runtime_id: str | None = None,
    protocol_family: str | None = None,
    capabilities: frozenset[str] = frozenset(),
    extra: dict[str, str] | None = None,
) -> BackendDescriptor:
    return BackendDescriptor(
        name=name,
        display_name=name,
        family=BackendFamily.CLI,
        backend_package=package,
        capabilities=frozenset(capabilities),
        protocol_family=protocol_family,
        runtime_id=runtime_id,
        extra_metadata=extra or {},
    )


def _install(
    monkeypatch: pytest.MonkeyPatch,
    descriptors: list[BackendDescriptor],
    impls: list[type],
) -> None:
    def loader(group: str):
        if group == "orchestratord.backend_descriptors":
            return [_FakeEp(d) for d in descriptors]
        return [_FakeEp(cls) for cls in impls]

    monkeypatch.setattr(
        "orchestratord.backend_registry._load_entry_points", loader
    )


@pytest.fixture(autouse=True)
def _reset_created():
    _PiImpl.created.clear()
    _OmpImpl.created.clear()
    yield


@pytest.fixture
def omp_world(monkeypatch: pytest.MonkeyPatch) -> dict[str, BackendDescriptor]:
    """The §8.2.2 canonical world: omp derives from pi, pi has an impl."""
    pi_desc = _descriptor("pi", package=_module_root(_PiImpl))
    omp_desc = _descriptor(
        "omp",
        package="orchestratord-omp",
        runtime_id="pi",
        protocol_family="pi",
    )
    _install(monkeypatch, [pi_desc, omp_desc], [_PiImpl])
    return {"pi": pi_desc, "omp": omp_desc}


class TestResolveDerivation:
    def test_derived_runtime_resolves_through_source(self, omp_world) -> None:
        backend = resolve_backend("omp")
        assert backend.name == "pi"

    def test_own_impl_wins_when_registered(
        self, omp_world, monkeypatch
    ) -> None:
        omp_with_impl = replace(
            omp_world["omp"], backend_package=_module_root(_OmpImpl)
        )
        # Both fakes share the ``tests`` module root, so the omp impl must
        # come first for the package matcher to pick it.
        _install(monkeypatch, [omp_world["pi"], omp_with_impl], [_OmpImpl, _PiImpl])
        assert resolve_backend("omp").name == "omp"

    def test_prefer_forwarded_from_deriving_descriptor(
        self, omp_world, monkeypatch
    ) -> None:
        omp_with_hint = replace(
            omp_world["omp"], extra_metadata={"prefer": "omp-prefer"}
        )
        _install(monkeypatch, [omp_world["pi"], omp_with_hint], [_PiImpl])
        resolve_backend("omp")
        assert _PiImpl.created[-1].prefer == "omp-prefer"

    def test_prefer_falls_back_to_source_descriptor(
        self, omp_world, monkeypatch
    ) -> None:
        pi_with_hint = replace(
            omp_world["pi"], extra_metadata={"prefer": "pi-prefer"}
        )
        _install(monkeypatch, [pi_with_hint, omp_world["omp"]], [_PiImpl])
        resolve_backend("omp")
        assert _PiImpl.created[-1].prefer == "pi-prefer"

    def test_no_impl_and_no_runtime_id_raises(self, monkeypatch) -> None:
        solo = _descriptor("solo", package="orchestratord-solo")
        _install(monkeypatch, [solo], [])
        with pytest.raises(BackendNotFoundError):
            resolve_backend("solo")

    def test_runtime_id_unknown_target_raises(
        self, omp_world, monkeypatch
    ) -> None:
        ghost = replace(omp_world["omp"], runtime_id="ghost")
        _install(monkeypatch, [ghost], [])
        with pytest.raises(BackendNotFoundError):
            resolve_backend("omp")

    def test_strict_drift_checks_original_descriptor(
        self, omp_world, monkeypatch
    ) -> None:
        omp_streaming = replace(
            omp_world["omp"],
            capabilities=frozenset({"streaming_deltas"}),
        )
        _install(monkeypatch, [omp_world["pi"], omp_streaming], [_PiImpl])
        with pytest.raises(BackendMismatchError):
            resolve_backend("omp", strict=True)


class TestDeriveHelper:
    def test_returns_source_descriptor(self, omp_world) -> None:
        derived = _derive_implementation_descriptor(omp_world["omp"])
        assert derived is not None
        assert derived.name == "pi"

    def test_descriptor_without_derivation_returns_none(
        self, omp_world
    ) -> None:
        assert _derive_implementation_descriptor(omp_world["pi"]) is None

    def test_self_reference_returns_none(self, omp_world) -> None:
        loop = replace(
            omp_world["omp"],
            runtime_id="omp",
            protocol_family="omp",
        )
        assert _derive_implementation_descriptor(loop) is None


class TestListBackends:
    def test_exposes_protocol_fields(self, omp_world) -> None:
        entries = {e["name"]: e for e in list_backends()}
        assert entries["omp"]["protocol_family"] == "pi"
        assert entries["omp"]["runtime_id"] == "pi"
        assert entries["pi"]["protocol_family"] is None
        assert entries["pi"]["runtime_id"] is None
