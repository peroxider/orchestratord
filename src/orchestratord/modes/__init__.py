"""Collaboration mode registry for the orchestrator.

This package exposes the ``ModeRunner`` Protocol plus a small registry so
new modes (Pipeline, Coordinator, Debate) can be plugged in without
touching the orchestrator core. Phase-1 ships only the ``single`` mode,
which is a transparent wrapper over the existing ``AgentRunner.run``
loop — behavior is byte-identical to the pre-mode code path.

Public surface
--------------

* ``ModeRunner``    — Protocol every mode implementation must satisfy
* ``ModeDecision``  — dataclass returned by ``ModeSelector.choose``
* ``register``      — register a ``ModeRunner`` under a string key
* ``get``           — fetch a registered runner by key (or raise KeyError)
* ``available``     — list registered mode keys
* ``DEFAULT_MODE``  — fallback when ModeSelector fails or returns unknown
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .base import DEFAULT_MODE, ModeDecision, ModeRunner

if TYPE_CHECKING:
    pass

_registry: dict[str, ModeRunner] = {}


def register(key: str, runner: ModeRunner) -> None:
    """Register a ``ModeRunner`` under ``key``.

    Re-registering the same key overwrites the prior entry — this is
    intentional so tests / plugins can swap implementations cleanly.
    """
    _registry[key] = runner


def get(key: str) -> ModeRunner:
    """Fetch the runner for ``key``.

    Raises
    ------
    KeyError
        If ``key`` is not registered. Callers that want a graceful
        fallback should catch this and use ``DEFAULT_MODE``.
    """
    return _registry[key]


def available() -> list[str]:
    """Return the list of currently registered mode keys."""
    return sorted(_registry.keys())


__all__ = [
    "DEFAULT_MODE",
    "ModeDecision",
    "ModeRunner",
    "register",
    "get",
    "available",
]
