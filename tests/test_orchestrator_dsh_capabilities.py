"""Test for Scheme C: dsh capability bit flip.

The dsh backend advertises ``cost_reporting=True`` because the linked
``deepseek-harness-sdk`` exposes token usage on ``RunResult``. This
test pins that contract — flipping the bit back to False (or
forgetting to flip it from False) breaks the core's fallback path
choice and silently degrades cost telemetry.

Run after the ``orchestratord-dsh`` package is installed (``pip
install -e backends/orchestratord-dsh``); the test is skipped
otherwise so a missing dev install does not turn into a red CI.
"""

from __future__ import annotations

import importlib

import pytest
from orchestratord_dsh.backend import DshBackend


def test_dsh_capabilities_cost_reporting_is_true() -> None:
    """The dsh backend must advertise ``cost_reporting=True``.

    The corresponding core fallback path is "use a token estimator" —
    see ``src/orchestratord/spi/capabilities.py:32``.
    """
    backend = DshBackend()
    caps = backend.capabilities()
    assert caps.cost_reporting is True, (
        "dsh advertises cost_reporting=True because "
        "deepseek-harness-sdk RunResult exposes usage; flipping back "
        "to False makes the core fall back to its token estimator "
        "and silently breaks cost telemetry."
    )


def test_dsh_capabilities_baseline_unchanged() -> None:
    """Lock the rest of the capability matrix so future edits cannot
    accidentally regress bits unrelated to the streaming pump.
    """
    backend = DshBackend()
    caps = backend.capabilities()
    # The notification pump forwards assistant/chunk deltas as
    # they arrive, so real deltas (not pseudo-splits) reach the core.
    assert caps.streaming_deltas is True
    # Honesty: cross-process resume fails with "id collision"
    # (the runtime has no remount protocol for a persisted session).
    # resumable=True was a false declaration that suppressed the core's
    # degradation path; same-process multi-turn still works but that is
    # NOT what the bit promises.
    assert caps.resumable is False
    assert caps.interrupt is False
    assert caps.approval_hooks is False
    assert caps.parallel_sessions is True
    assert caps.tool_filtering is False
    assert caps.takeover is False


def test_dsh_session_optimistic_cost_default() -> None:
    """A freshly-constructed session advertises ``cost_reporting=True``
    before the harness probe runs (the optimistic default; see
    ``DshSession._probe_cost_support``).
    """
    from orchestratord_dsh.session import DshSession

    from orchestratord.spi.backend import SessionSpec

    session = DshSession(SessionSpec(cwd="/tmp"))
    assert session.capabilities.cost_reporting is True


def test_dsh_module_importable() -> None:
    """The dsh package must remain importable as the entry-point
    module name (``orchestratord_dsh``) so the registry can load it.
    """
    try:
        mod = importlib.import_module("orchestratord_dsh")
    except ModuleNotFoundError:
        pytest.skip("orchestratord-dsh not installed in this environment")
    assert hasattr(mod, "DshBackend")