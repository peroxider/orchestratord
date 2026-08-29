"""Shared title-prefix filtering for issue tracker adapters."""

from __future__ import annotations

from typing import Iterable

TITLE_PREFIX_MATCH_MODES = frozenset({"any", "all"})


def normalize_title_prefixes(prefixes: Iterable[object] | None) -> tuple[str, ...]:
    """Return non-empty title prefixes while preserving their configured order."""
    return tuple(
        prefix.strip()
        for prefix in (prefixes or [])
        if isinstance(prefix, str) and prefix.strip()
    )


def normalize_title_prefix_match(value: object) -> str:
    """Normalize a title-prefix mode; invalid values safely use ``any``."""
    mode = str(value or "any").strip().lower()
    return mode if mode in TITLE_PREFIX_MATCH_MODES else "any"


def matches_title_prefixes(title: str | None, prefixes: Iterable[str], mode: str) -> bool:
    """Whether ``title`` matches configured prefixes.

    An empty prefix collection disables filtering.  ``any`` is union/OR
    semantics; ``all`` is intersection/AND semantics.
    """
    prefixes = tuple(prefixes)
    if not prefixes:
        return True
    candidate = title or ""
    checks = (candidate.startswith(prefix) for prefix in prefixes)
    return all(checks) if mode == "all" else any(checks)
