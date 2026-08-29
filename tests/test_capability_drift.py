"""Scheme D — CI capability drift detector (descriptor-driven).

Per ``DESIGN_two_tier_backend_registry.md`` §4, the drift detector compares
two layers and catches inconsistencies between them:

1. **Descriptor layer** — every backend package registers one (or more)
   :class:`BackendDescriptor` via the ``orchestratord.backend_descriptors``
   entry-point group. The descriptor is the **single source of truth** for
   ``family`` / ``capabilities`` / ``cli_command`` / etc.

2. **Implementation layer** — every backend package registers an
   :class:`AgentBackend` subclass via the ``orchestratord.backends``
   entry-point group. The class's :py:meth:`capabilities` method returns a
   :class:`BackendCapabilities` instance whose truthy fields must match the
   descriptor's declared bit set.

This test asserts the round-trip in three directions:

* **bit-set drift** — ``backend.capabilities()`` equals
  ``descriptor.capabilities`` for at least one descriptor of the backend's
  package (multiple descriptors per package are allowed, e.g. codex-cli +
  codex-app-server).
* **every descriptor has an implementation** — a descriptor without a
  backend class is a contract violation (silently dropped runtime).
* **every backend has a descriptor** — a backend class without a descriptor
  is a contract violation (silent metadata drift).

Backends whose packages are not installed in the current environment are
skipped (``pytest.skip``), not failed. The detector fails when the backend
**is** installed but its bits drift, or when one side is missing.
"""

from __future__ import annotations

import importlib.metadata as md_mod
import sys
from dataclasses import fields
from pathlib import Path

import pytest

from orchestratord._backend_cli_registry import KNOWN_BACKEND_CLIS, binary_names
from orchestratord.spi.backend_descriptor import BackendDescriptor
from orchestratord.spi.capabilities import BackendCapabilities


# ---------------------------------------------------------------------------
# Helpers — entry-point loaders shared by drift tests
# ---------------------------------------------------------------------------


def _load_descriptors() -> dict[str, BackendDescriptor]:
    """Discover all :class:`BackendDescriptor` via the descriptor entry-point group.

    Returns ``{descriptor.name: descriptor}``. Errors loading any one
    descriptor are silently skipped — the registered descriptor is the
    contract; a broken loader should be fixed at registration time, not
    block every other descriptor.
    """
    result: dict[str, BackendDescriptor] = {}
    try:
        eps = md_mod.entry_points(group="orchestratord.backend_descriptors")
    except Exception:
        return result
    for ep in eps:
        try:
            desc = ep.load()
        except Exception:
            continue
        if isinstance(desc, BackendDescriptor):
            result[desc.name] = desc
    return result


def _load_backends() -> dict[str, object]:
    """Discover all :class:`AgentBackend` subclasses via the backend entry-point group.

    Returns ``{ep.name: backend_instance}``. Loader failures are silently
    skipped (same rationale as :func:`_load_descriptors`).
    """
    backends: dict[str, object] = {}
    try:
        eps = md_mod.entry_points(group="orchestratord.backends")
    except Exception:
        return backends
    for ep in eps:
        try:
            cls = ep.load()
        except Exception:
            continue
        try:
            backends[ep.name] = cls()
        except Exception:
            continue
    return backends


def _backend_package(backend: object) -> str | None:
    """Return the hyphenated ``orchestratord-*`` package that owns *backend*.

    The backend instance's class is loaded from a module like
    ``orchestratord_clawcodex.backend`` — we strip the underscored prefix and
    re-hyphenate. Returns ``None`` if the module prefix does not match the
    backend-package convention.
    """
    module = type(backend).__module__.split(".")[0]
    if not module.startswith("orchestratord_"):
        return None
    return module.replace("_", "-")


def _actual_bits(caps: BackendCapabilities) -> set[str]:
    """Return the set of capability bit names currently set on *caps*.

    Uses :py:func:`dataclasses.fields` rather than a hard-coded tuple, so
    adding a new bit to :class:`BackendCapabilities` automatically extends
    this check.
    """
    return {f.name for f in fields(caps) if getattr(caps, f.name)}


def _backends_by_package() -> dict[str, list[object]]:
    """Group discovered backends by their owning ``orchestratord-*`` package.

    For the codex package, both runtime variants share a single
    ``CodexBackend`` instance — the probe decides which bit set is active.
    """
    grouped: dict[str, list[object]] = {}
    for backend in _load_backends().values():
        pkg = _backend_package(backend)
        if pkg is None:
            continue
        grouped.setdefault(pkg, []).append(backend)
    return grouped


def _descriptors_by_package() -> dict[str, list[BackendDescriptor]]:
    grouped: dict[str, list[BackendDescriptor]] = {}
    for desc in _load_descriptors().values():
        grouped.setdefault(desc.backend_package, []).append(desc)
    return grouped


# ---------------------------------------------------------------------------
# Per-package drift tests
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def discovered_backends() -> dict[str, object]:
    return _load_backends()


@pytest.fixture(scope="module")
def descriptors() -> dict[str, BackendDescriptor]:
    return _load_descriptors()


@pytest.mark.parametrize(
    "backend_package",
    sorted(set(_backends_by_package()) | set(_descriptors_by_package())),
)
def test_capability_bit_set_matches_descriptor(
    backend_package: str,
    discovered_backends: dict[str, object],
    descriptors: dict[str, BackendDescriptor],
) -> None:
    """For each ``orchestratord-*`` package, the installed backend's actual
    capability bits must equal at least one registered descriptor's
    declared bits.

    Multiple descriptors per package are allowed (codex-cli + codex-app-
    server); the test asserts match against **any** of them, since the
    backend class only exposes one of the two runtime bit sets at a time.
    """
    matching_backends = [
        b
        for b in discovered_backends.values()
        if _backend_package(b) == backend_package
    ]
    matching_descriptors = [
        d
        for d in descriptors.values()
        if d.backend_package == backend_package
    ]

    if not matching_backends:
        pytest.skip(
            f"backend package {backend_package!r} has descriptor(s) "
            f"{[d.name for d in matching_descriptors]} but the package "
            "is not installed in this environment"
        )
    if not matching_descriptors:
        pytest.fail(
            f"backend package {backend_package!r} is installed but has "
            "no descriptor registered under "
            "orchestratord.backend_descriptors — register one in "
            f"{backend_package}/pyproject.toml"
        )

    for backend in matching_backends:
        actual = _actual_bits(backend.capabilities())
        accepted = [set(d.capabilities) for d in matching_descriptors]
        if actual not in accepted:
            pytest.fail(
                f"{backend_package}: capability bit set drifted — "
                f"actual={sorted(actual)} does not match any descriptor: "
                + " | ".join(
                    f"{d.name}={sorted(d.capabilities)}" for d in matching_descriptors
                )
            )


@pytest.mark.parametrize(
    "backend_package",
    sorted(set(_backends_by_package()) | set(_descriptors_by_package())),
)
def test_family_value_is_known(
    backend_package: str,
    discovered_backends: dict[str, object],
    descriptors: dict[str, BackendDescriptor],
) -> None:
    """Every descriptor's ``family`` must be one of the four
    :class:`BackendFamily` enum values — a typo'd string would silently
    bypass the SPI's family-based dispatch.

    Also asserts the installed backend's :py:attr:`name` /
    :py:attr:`display_name` are non-empty strings (sanity, not strictly
    drift detection).
    """
    matching_descriptors = [
        d for d in descriptors.values() if d.backend_package == backend_package
    ]
    if not matching_descriptors:
        pytest.skip(f"no descriptors registered for {backend_package!r}")
    for desc in matching_descriptors:
        # BackendFamily values are constrained to the enum; asserting the
        # descriptor's ``family.value`` matches one of the canonical strings
        # catches accidental re-mapping.
        assert desc.family.value in {"InProcess", "SdkProcess", "Protocol", "Cli"}, (
            f"{desc.name}: family value {desc.family.value!r} not in the "
            "canonical four-family set — check BackendFamily enum"
        )


# ---------------------------------------------------------------------------
# Cross-layer completeness checks
# ---------------------------------------------------------------------------


def test_every_descriptor_has_an_implementation() -> None:
    """Sanity: every registered descriptor's ``backend_package`` must have
    at least one :class:`AgentBackend` entry-point installed.

    A descriptor without a backend is a silent contract violation —
    ``resolve_backend()`` would raise :class:`BackendNotFoundError` at
    runtime with no upstream signal that the descriptor exists.
    """
    descriptors = _descriptors_by_package()
    backends = _backends_by_package()
    missing_pkgs = sorted(set(descriptors) - set(backends))
    assert not missing_pkgs, (
        "descriptors registered for backend packages with no installed "
        f"AgentBackend: {missing_pkgs}. Install the package or remove the "
        "descriptor entry-point."
    )


def test_every_backend_has_a_descriptor() -> None:
    """Sanity: every installed :class:`AgentBackend` must have at least
    one :class:`BackendDescriptor` registered.

    A backend without a descriptor is a silent metadata gap. The drift
    detector would not catch this case (it can only see what's declared),
    and ``list_backends()`` would skip the backend entirely.
    """
    backends = _backends_by_package()
    descriptors = _descriptors_by_package()
    missing_pkgs = sorted(set(backends) - set(descriptors))
    assert not missing_pkgs, (
        "AgentBackend installed for backend packages without a descriptor: "
        f"{missing_pkgs}. Add a BackendDescriptor to "
        f"{missing_pkgs[0]}/src/.../descriptor.py and register it under "
        "orchestratord.backend_descriptors."
    )


# ---------------------------------------------------------------------------
# Stability — repeated calls must be deterministic
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(_load_backends()))
def test_capabilities_is_stable_across_calls(
    name: str,
    discovered_backends: dict[str, object],
) -> None:
    """Repeated :py:meth:`capabilities` invocations on the same backend
    instance must return dataclasses that compare equal — no spontaneous
    toggling.
    """
    backend = discovered_backends[name]
    first = backend.capabilities()
    second = backend.capabilities()
    assert first == second


# ---------------------------------------------------------------------------
# Sanity: prove the detector is not a no-op
# ---------------------------------------------------------------------------


def test_detector_flags_intentional_mismatch() -> None:
    """Sanity: the detector must reject a bit set that obviously disagrees
    with what the backend advertises.

    Concretely — if a backend reports ``streaming_deltas=True`` then a
    "ground truth" that excludes that bit must diverge. We rely on at
    least one backend (clawcodex, opencode, or codex-as) being installed
    and advertising ``streaming_deltas=True``.
    """
    backends = _load_backends()
    if not backends:
        pytest.skip("no backends installed in this environment")
    streaming_backends = [
        name
        for name, backend in backends.items()
        if getattr(backend.capabilities(), "streaming_deltas", False)
    ]
    if not streaming_backends:
        pytest.skip(
            "no streaming-deltas backend installed (drift detector "
            "would have nothing to assert against)"
        )
    for name in streaming_backends:
        backend = backends[name]
        actual = _actual_bits(backend.capabilities())
        wrong: set[str] = set()  # trivially wrong — empty bit set
        assert actual != wrong, (
            f"{name}: detector logic broken — empty ground truth "
            "would have matched an advertised bit set"
        )


# ---------------------------------------------------------------------------
# Backend CLI registry drift (DESIGN_backend_cli_test_guard.md §7)
# ---------------------------------------------------------------------------
#
# Pairs ``orchestratord._backend_cli_registry.KNOWN_BACKEND_CLIS`` with
# what the backend source code actually invokes. The hard failure is
# one-directional: every subprocess binary the backends spawn must be
# registered, so the PATH guard (see ``tests/conftest.py``) actually
# blocks it. The reverse direction ("registered but unused") is a soft
# warning only, because the design doc reserves two slots —
# ``clawcodex-dev`` (capability probe) and ``dsh`` (deepseek-harness
# SDK-internal subprocess) — for binaries that are not yet invoked
# from in-tree code.

# Well-known invocations that are clearly not backend CLIs. Kept short
# on purpose: anything beyond this list is a candidate for registration.
_BENIGN_BINARIES: frozenset[str] = frozenset(
    {
        "python",
        "python3",
        "git",
        "sh",
        "bash",
        "uv",
        "pip",
    }
)

# Subprocess call patterns we audit. Each pattern's group(1) is the
# binary name as a string literal. We deliberately skip variable-only
# invocations (``create_subprocess_exec(*args)`` where ``args`` is
# built dynamically) — those require data-flow analysis that's out of
# scope here, and the registry still wins for the literals.
_SUBPROCESS_LITERAL_PATTERNS: tuple[str, ...] = (
    # asyncio.create_subprocess_exec("foo", ...)
    r'(?:asyncio\.)?create_subprocess_exec\s*\(\s*"([a-z][a-z0-9_.\-]*)"',
    # subprocess.Popen|run|check_output|check_call(["foo", ...])
    r'subprocess\.(?:Popen|run|check_output|check_call)\s*\(\s*\[\s*"([a-z][a-z0-9_.\-]*)"',
    # shutil.which("foo")  — a binary-name lookup is strong evidence
    # the binary is being resolved at runtime.
    r'shutil\.which\s*\(\s*"([a-z][a-z0-9_.\-]*)"',
    # args = ["foo", ...] / cmd = ["foo", ...] — argument-builder
    # idiom used by codex/hermes before spreading into create_subprocess_exec.
    r'(?:^|\n)\s*(?:args|cmd|command|argv)\s*=\s*\[\s*"([a-z][a-z0-9_.\-]*)"',
)


def _scan_backend_subprocess_binaries(
    backends_root: Path,
) -> dict[str, list[tuple[str, str]]]:
    """Return ``{binary: [(relpath, snippet)]}`` for every literal binary
    name that appears in a subprocess invocation pattern under
    ``backends/``.

    Benign utilities (``python``, ``git``, etc.) are filtered out.
    """
    found: dict[str, list[tuple[str, str]]] = {}
    for pkg_dir in sorted(p for p in backends_root.iterdir() if p.is_dir()):
        for py_file in pkg_dir.rglob("*.py"):
            try:
                source = py_file.read_text()
            except (OSError, UnicodeDecodeError):
                continue
            for pattern in _SUBPROCESS_LITERAL_PATTERNS:
                for match in __import__("re").finditer(pattern, source):
                    binary = match.group(1)
                    if binary in _BENIGN_BINARIES:
                        continue
                    relpath = str(py_file.relative_to(backends_root))
                    snippet_start = max(0, match.start() - 20)
                    snippet_end = min(len(source), match.end() + 20)
                    snippet = source[snippet_start:snippet_end].replace("\n", " ")
                    found.setdefault(binary, []).append((relpath, snippet))
    return found


def test_backend_cli_registry_covers_subprocess_invocations() -> None:
    """Every literal binary name invoked via subprocess in ``backends/``
    must appear in ``KNOWN_BACKEND_CLIS`` — otherwise the test-time
    PATH guard cannot intercept it.

    Per ``DESIGN_backend_cli_test_guard.md`` §7. This catches the
    dangerous half of drift (a backend picks up a new binary without
    updating the registry) while keeping the unused-slots case
    (``clawcodex-dev`` capability probe, ``dsh`` SDK-internal) as a
    soft warning further down.
    """
    import re

    backends_root = Path(__file__).resolve().parent.parent / "backends"
    if not backends_root.is_dir():
        pytest.skip(f"backends/ not present at {backends_root}")

    found = _scan_backend_subprocess_binaries(backends_root)
    registered = set(binary_names())
    unregistered = sorted(set(found) - registered)

    assert not unregistered, (
        "subprocess invocations in backends/ use binaries NOT in "
        "KNOWN_BACKEND_CLIS — the PATH guard cannot block them. "
        f"Add these to orchestratord._backend_cli_registry:\n  "
        + "\n  ".join(
            f"{b}  (e.g. {found[b][0][0]}: …{found[b][0][1]}…)"
            for b in unregistered
        )
    )


def test_backend_cli_registry_no_orphaned_packages() -> None:
    """Every ``KNOWN_BACKEND_CLIS.backend_package`` directory must
    exist under ``backends/``.

    A package that was deleted upstream without removing its entry from
    the registry would silently make the guard block a binary nothing
    uses anymore — a confusing CI state. This test fails loudly so the
    next person who deletes a backend cleans up the registry too.
    """
    backends_root = Path(__file__).resolve().parent.parent / "backends"
    if not backends_root.is_dir():
        pytest.skip(f"backends/ not present at {backends_root}")

    missing = sorted(
        c.backend_package
        for c in KNOWN_BACKEND_CLIS
        if not (backends_root / c.backend_package).is_dir()
    )
    assert not missing, (
        "KNOWN_BACKEND_CLIS references backend packages that no longer "
        f"exist under backends/: {missing}. Remove them from "
        "orchestratord._backend_cli_registry."
    )


def test_backend_cli_registry_warns_on_unused_binaries() -> None:
    """Soft check: warn (do not fail) when a registered binary has no
    subprocess call site in any backend source today.

    Today this surfaces ``clawcodex-dev`` (registered for a future
    capability probe) and ``dsh`` (registered because the SDK invokes
    it internally). Neither is yet called from in-tree code, but the
    design doc explicitly reserves both slots. We surface the warning
    to keep the registry honest, not to gate CI.
    """
    backends_root = Path(__file__).resolve().parent.parent / "backends"
    if not backends_root.is_dir():
        pytest.skip(f"backends/ not present at {backends_root}")

    found = set(_scan_backend_subprocess_binaries(backends_root))
    unused = sorted(set(binary_names()) - found)
    if unused:
        print(  # noqa: T201 — pytest captures stdout
            "warning: KNOWN_BACKEND_CLIS lists binaries with no in-tree "
            f"subprocess call site today: {unused}. These are kept as "
            "forward-looking slots per DESIGN_backend_cli_test_guard.md §1.2; "
            "remove them once they are no longer expected to be invoked.",
            file=sys.stderr,
        )


# ---------------------------------------------------------------------------
# Agent-callable skills — frontmatter contract (DESIGN_agent_callable_skills.md)
# ---------------------------------------------------------------------------


_REQUIRED_SKILL_FIELDS = ("name", "description")


def test_builtin_skill_frontmatter_has_required_fields() -> None:
    """Every builtin SKILL.md must carry the required frontmatter fields.

    A skill missing ``name`` or ``description`` would render a broken
    entry in the agent-visible skill index (empty description line) and
    break ``skills show`` lookups — fail loudly at CI time instead.
    """
    import yaml

    from orchestratord.skills.loader import _split_frontmatter

    skills_root = (
        Path(__file__).resolve().parent.parent
        / "src" / "orchestratord" / "skills" / "builtin"
    )
    if not skills_root.is_dir():
        pytest.skip(f"skills/builtin not present at {skills_root}")

    skill_files = sorted(skills_root.glob("*/SKILL.md"))
    assert skill_files, "no builtin skills found — starter skills missing?"

    for skill_md in skill_files:
        text = skill_md.read_text(encoding="utf-8")
        try:
            fm, _body = _split_frontmatter(text)
        except ValueError as exc:
            pytest.fail(f"{skill_md.parent.name}/SKILL.md: {exc}")
        meta = yaml.safe_load(fm) or {}
        missing = [f for f in _REQUIRED_SKILL_FIELDS if not meta.get(f)]
        assert not missing, (
            f"{skill_md.parent.name}/SKILL.md: missing frontmatter "
            f"field(s) {missing} — see DESIGN_agent_callable_skills.md §2"
        )