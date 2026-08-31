"""Scheme A invariants — :class:`BackendDescriptor` dataclass contract.

Per ``DESIGN_two_tier_backend_registry.md`` §1.3, the descriptor must be:

* a frozen dataclass (immutable — descriptors are pure declarations)
* field-typed so a typo at the call site fails at import time
* export the :class:`BackendFamily` enum with the four canonical values
  ``InProcess`` / ``SdkProcess`` / ``Protocol`` / ``Cli``

This test pins those invariants. It does NOT depend on any backend package
being installed — the descriptor is purely a core-SPI construct.
"""

from __future__ import annotations

import dataclasses
import enum

import pytest

from orchestratord.spi.backend_descriptor import BackendDescriptor, BackendFamily


# ---------------------------------------------------------------------------
# BackendFamily enum invariants
# ---------------------------------------------------------------------------


def test_backend_family_has_canonical_four_values() -> None:
    """The enum exposes exactly ``InProcess`` / ``SdkProcess`` / ``Protocol`` / ``Cli``.

    These four strings are part of the public contract: ``list_backends()``
    historically emitted them as ``family`` field values, and downstream
    dispatchers (CLI / dashboard) key on the strings.
    """
    actual = {member.value for member in BackendFamily}
    assert actual == {"InProcess", "SdkProcess", "Protocol", "Cli"}, (
        f"BackendFamily values drifted: {actual}. Adding a new family "
        "requires updating downstream dispatchers."
    )


def test_backend_family_is_pure_enum() -> None:
    """Sanity: ``BackendFamily`` is a subclass of :class:`enum.Enum`, not
    a ``StrEnum``/``IntEnum`` derivative that might break downstream
    string comparisons.
    """
    assert issubclass(BackendFamily, enum.Enum)
    assert not issubclass(BackendFamily, str)
    assert not issubclass(BackendFamily, int)


def test_backend_family_member_count_matches_design() -> None:
    """Catches accidental add/remove of enum members without ADR update."""
    assert len(BackendFamily) == 4, (
        f"BackendFamily has {len(BackendFamily)} members, expected 4. "
        "Adding/removing a family is a breaking change."
    )


# ---------------------------------------------------------------------------
# BackendDescriptor dataclass invariants
# ---------------------------------------------------------------------------


def test_descriptor_is_a_frozen_dataclass() -> None:
    """Descriptors must be immutable — replacing fields at runtime would
    silently bypass drift detection. ``frozen=True`` raises
    :class:`dataclasses.FrozenInstanceError` on attribute assignment.
    """
    assert dataclasses.is_dataclass(BackendDescriptor)
    assert BackendDescriptor.__dataclass_params__.frozen is True  # type: ignore[attr-defined]


def test_descriptor_has_required_fields() -> None:
    """Sanity: every field declared in §1.2 must exist on the dataclass.

    Field names are part of the public contract — descriptor entry-points
    pass field values by keyword in each backend's ``descriptor.py``.
    Renaming a field is a breaking change requiring migration of all five
    descriptors.
    """
    field_names = {f.name for f in dataclasses.fields(BackendDescriptor)}
    expected = {
        "name",
        "display_name",
        "family",
        "backend_package",
        "capabilities",
        "cli_command",
        "cli_args_probe",
        "env_prefix",
        "launch_header",
        "model_discovery",
        "extra_metadata",
    }
    assert field_names == expected, (
        f"BackendDescriptor fields drifted: missing={expected - field_names}, "
        f"extra={field_names - expected}"
    )


def test_descriptor_capabilities_field_type_is_frozenset() -> None:
    """The ``capabilities`` field must be ``frozenset[str]`` (immutable
    hashable set) so descriptors can be hashed/cached and so accidental
    mutation raises immediately.
    """
    caps_field = next(
        f for f in dataclasses.fields(BackendDescriptor) if f.name == "capabilities"
    )
    type_str = str(caps_field.type)
    assert "frozenset" in type_str.lower(), (
        f"capabilities field type is {type_str!r}; expected frozenset[str]"
    )


def test_descriptor_default_values_match_design() -> None:
    """§1.2 specifies defaults for optional fields. Re-asserting them
    here guards against silent default changes that would change
    behaviour for every existing descriptor.
    """
    minimal = BackendDescriptor(
        name="x",
        display_name="X",
        family=BackendFamily.CLI,
        backend_package="pkg-x",
        capabilities=frozenset(),
    )
    assert minimal.cli_command is None
    assert minimal.cli_args_probe == ()
    assert minimal.env_prefix is None
    assert minimal.launch_header is None
    assert minimal.model_discovery == "user"
    assert minimal.extra_metadata == {}


def test_descriptor_supports_empty_capabilities() -> None:
    """A backend with **no** declared bits is allowed (e.g. a placeholder
    runtime). The descriptor must accept an empty frozenset without
    complaining.
    """
    desc = BackendDescriptor(
        name="placeholder",
        display_name="Placeholder",
        family=BackendFamily.PROTOCOL,
        backend_package="pkg-placeholder",
        capabilities=frozenset(),
    )
    assert desc.capabilities == frozenset()


def test_descriptor_rejects_mutation() -> None:
    """Per the frozen contract, attribute assignment must raise.

    This catches a regression where someone removes ``frozen=True``
    thinking it's harmless — descriptors are configuration loaded
    once at startup, and a single in-place mutation would silently
    desynchronize the drift guard from the actual backend.
    """
    desc = BackendDescriptor(
        name="x",
        display_name="X",
        family=BackendFamily.CLI,
        backend_package="pkg-x",
        capabilities=frozenset(),
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        desc.name = "y"  # type: ignore[misc]


@pytest.mark.parametrize(
    "family",
    list(BackendFamily),
)
def test_descriptor_accepts_every_family_member(family: BackendFamily) -> None:
    """Each :class:`BackendFamily` member must construct a valid
    descriptor — the enum is the public contract for "which family
    strings are allowed".
    """
    desc = BackendDescriptor(
        name=f"x-{family.value.lower()}",
        display_name=family.value,
        family=family,
        backend_package="pkg-x",
        capabilities=frozenset({"resumable"}),
    )
    assert desc.family is family
    assert desc.family.value == family.value


def test_descriptor_model_discovery_literal_values() -> None:
    """``model_discovery`` must accept only ``"static"`` / ``"probe"`` /
    ``"user"`` — a typo silently falls through to wrong model-list
    resolution, which the drift guard cannot catch at import time.
    """
    for value in ("static", "probe", "user"):
        desc = BackendDescriptor(
            name=f"x-{value}",
            display_name="X",
            family=BackendFamily.CLI,
            backend_package="pkg-x",
            capabilities=frozenset(),
            model_discovery=value,  # type: ignore[arg-type]
        )
        assert desc.model_discovery == value


def test_descriptor_hashable() -> None:
    """Frozen dataclasses with frozenset fields are hashable. This lets
    downstream caches key descriptors in dicts / lru_cache without
    converting to tuples.
    """
    desc = BackendDescriptor(
        name="x",
        display_name="X",
        family=BackendFamily.CLI,
        backend_package="pkg-x",
        capabilities=frozenset({"resumable"}),
    )
    hash(desc)


def test_descriptor_equality_is_value_based() -> None:
    """Two descriptors constructed with identical fields compare equal —
    the drift guard relies on this for cross-layer checks.
    """
    common = dict(
        name="x",
        display_name="X",
        family=BackendFamily.CLI,
        backend_package="pkg-x",
        capabilities=frozenset({"resumable"}),
        env_prefix="X_",
    )
    assert BackendDescriptor(**common) == BackendDescriptor(**common)


def test_descriptor_inequality_on_field_difference() -> None:
    """Single-field difference must yield inequality — guards against an
    accidental ``eq=False`` override.
    """
    base = BackendDescriptor(
        name="x",
        display_name="X",
        family=BackendFamily.CLI,
        backend_package="pkg-x",
        capabilities=frozenset(),
    )
    other = BackendDescriptor(
        name="x",
        display_name="X",
        family=BackendFamily.IN_PROCESS,
        backend_package="pkg-x",
        capabilities=frozenset(),
    )
    assert base != other


def test_descriptor_exported_from_spi() -> None:
    """Public SPI symbols must be re-exported from
    :mod:`orchestratord.spi`. ``resolve_backend()`` and downstream
    dispatchers import via this top-level path; re-exporting here
    keeps the public surface coherent.
    """
    from orchestratord.spi import BackendDescriptor as ReExported
    from orchestratord.spi import BackendFamily as ReExportedFamily

    assert ReExported is BackendDescriptor
    assert ReExportedFamily is BackendFamily